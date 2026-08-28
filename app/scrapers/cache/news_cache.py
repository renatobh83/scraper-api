import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timezone

from app.models import NewsResponse
from app.scrapers.base import ScraperError

logger = logging.getLogger(__name__)


@dataclass
class CacheEntry:
    created_at: float
    result: NewsResponse
    refreshing: bool = False
    failures: int = 0
    next_retry_at: float = 0.0


class NewsCache:
    """
    Cache com stale-while-revalidate.

    - fresh: retorna cache imediatamente.
    - stale: retorna cache antigo e atualiza em background.
    - sem cache: faz atualização síncrona.
    - erros: usa retry/backoff.
    """

    def __init__(
        self,
        fresh_ttl: int = 60,
        stale_ttl: int = 600,
        base_retry: int = 30,
        max_retry: int = 300,
        max_attempts: int = 3,
    ):
        self.fresh_ttl = fresh_ttl
        self.stale_ttl = stale_ttl
        self.base_retry = base_retry
        self.max_retry = max_retry
        self.max_attempts = max_attempts

        self._cache: dict[tuple[str, int], CacheEntry] = {}
        self._locks: dict[tuple[str, int], asyncio.Lock] = {}

    def _get_lock(self, key: tuple[str, int]) -> asyncio.Lock:
        lock = self._locks.get(key)

        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock

        return lock

    def _now(self) -> float:
        return asyncio.get_running_loop().time()

    def get(
        self,
        key: tuple[str, int],
    ) -> tuple[NewsResponse | None, bool]:
        """
        Retorna:

        (resultado, is_fresh)

        is_fresh=True  -> cache dentro do TTL.
        is_fresh=False -> cache stale.
        """

        entry = self._cache.get(key)

        if entry is None:
            return None, False

        age = self._now() - entry.created_at

        if age <= self.fresh_ttl:
            return entry.result, True

        if age <= self.stale_ttl:
            return entry.result.model_copy(update={"stale": True}), False

        return None, False

    async def refresh(
        self,
        key: tuple[str, int],
        scraper,
        context,
        limit: int,
    ) -> NewsResponse | None:

        lock = self._get_lock(key)

        async with lock:
            entry = self._cache.get(key)
            now = self._now()

            # Outra coroutine pode ter atualizado enquanto
            # esperávamos pelo lock.
            if entry:
                age = now - entry.created_at

                if age <= self.fresh_ttl:
                    return entry.result

            # Evita retry antes da hora.
            if entry and now < entry.next_retry_at:
                logger.info(
                    "[NEWS] Retry bloqueado por backoff: " "source=%s retry_em=%.1fs",
                    scraper.source_name,
                    entry.next_retry_at - now,
                )

                return (
                    entry.result.model_copy(update={"stale": True}) if entry else None
                )

            if entry:
                entry.refreshing = True

            try:
                for attempt in range(1, self.max_attempts + 1):

                    try:
                        logger.info(
                            "[NEWS] Atualizando cache: " "source=%s tentativa=%s/%s",
                            scraper.source_name,
                            attempt,
                            self.max_attempts,
                        )

                        items = await scraper.scrape(
                            context,
                            limit,
                        )

                        result = NewsResponse(
                            source=scraper.source_name,
                            fetched_at=datetime.now(timezone.utc),
                            count=len(items),
                            items=list(items),
                            stale=False,
                        )

                        self._cache[key] = CacheEntry(
                            created_at=self._now(),
                            result=result,
                            refreshing=False,
                            failures=0,
                            next_retry_at=0,
                        )

                        logger.info(
                            "[NEWS] Cache atualizado com sucesso: "
                            "source=%s count=%s",
                            scraper.source_name,
                            len(items),
                        )

                        return result

                    except ScraperError as exc:

                        logger.warning(
                            "[NEWS] Falha temporária: "
                            "source=%s tentativa=%s/%s erro=%s",
                            scraper.source_name,
                            attempt,
                            self.max_attempts,
                            exc,
                        )

                        if attempt >= self.max_attempts:
                            break

                        # Exponential backoff:
                        #
                        # tentativa 1 → ~30s
                        # tentativa 2 → ~60s
                        #
                        # com jitter para não ficar sempre exatamente
                        # no mesmo intervalo.
                        delay = min(
                            self.base_retry * (2 ** (attempt - 1)),
                            self.max_retry,
                        )

                        jitter = random.uniform(
                            delay * 0.2,
                            delay * 0.5,
                        )

                        wait_time = delay + jitter

                        logger.info(
                            "[NEWS] Próxima tentativa em %.1fs",
                            wait_time,
                        )

                        await asyncio.sleep(wait_time)

                    except Exception as exc:

                        logger.exception(
                            "[NEWS] Erro inesperado: source=%s",
                            scraper.source_name,
                        )

                        if attempt >= self.max_attempts:
                            break

                        delay = min(
                            self.base_retry * (2 ** (attempt - 1)),
                            self.max_retry,
                        )

                        await asyncio.sleep(delay + random.uniform(0, delay * 0.3))

                # Todas as tentativas falharam.

                if entry:
                    entry.refreshing = False
                    entry.failures += 1

                    backoff = min(
                        self.base_retry * (2**entry.failures),
                        self.max_retry,
                    )

                    entry.next_retry_at = self._now() + backoff

                    logger.warning(
                        "[NEWS] Mantendo cache stale após falha: "
                        "source=%s failures=%s retry_em=%ss",
                        scraper.source_name,
                        entry.failures,
                        backoff,
                    )

                    return entry.result.model_copy(update={"stale": True})

                return None

            finally:
                entry = self._cache.get(key)

                if entry:
                    entry.refreshing = False

    def start_background_refresh(
        self,
        key: tuple[str, int],
        scraper,
        context,
        limit: int,
    ) -> None:
        """
        Dispara atualização em background.

        Não espera a atualização terminar.
        """

        entry = self._cache.get(key)

        if entry and entry.refreshing:
            logger.debug(
                "[NEWS] Refresh já está em andamento: %s",
                key,
            )
            return

        if entry:
            entry.refreshing = True

        asyncio.create_task(
            self._background_refresh(
                key,
                scraper,
                context,
                limit,
            )
        )

    async def _background_refresh(
        self,
        key,
        scraper,
        context,
        limit,
    ):
        try:
            await self.refresh(
                key=key,
                scraper=scraper,
                context=context,
                limit=limit,
            )

        except Exception:
            logger.exception(
                "[NEWS] Erro no refresh em background: source=%s",
                scraper.source_name,
            )

    async def clear(self):
        self._cache.clear()
        self._locks.clear()


# ============================================================
# CONFIGURAÇÃO
# ============================================================

news_cache = NewsCache(
    fresh_ttl=60,  # 1 minuto de cache fresco
    stale_ttl=600,  # mantém cache antigo por até 10 minutos
    base_retry=30,  # primeira espera: ~30s
    max_retry=300,  # máximo: 5 minutos
    max_attempts=3,
)
