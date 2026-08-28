import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from fastapi.responses import JSONResponse
from fastapi import FastAPI, HTTPException, Query

from app.browser import BrowserManager
from app.config import settings
from app.models import HealthResponse, NewsResponse
from app.scrapers import SCRAPERS
from app.scrapers.base import ScraperError

logger = logging.getLogger(__name__)


# ============================================================
# RECURSOS GLOBAIS
# ============================================================

scrape_semaphore = asyncio.Semaphore(settings.max_concurrent_scrapes)

# Cache simples:
# {
#     ("investing", 10): (
#         timestamp,
#         NewsResponse(...)
#     )
# }
cache: dict[tuple[str, int], tuple[float, NewsResponse]] = {}
blocked_until: dict[tuple[str, int], float] = {}
browser_manager = BrowserManager()
background_refresh_tasks: dict[tuple[str, int], asyncio.Task] = {}


async def background_retry_news(
    cache_key: tuple[str, int],
    scraper,
    limit: int,
):
    """
    Tenta atualizar as notícias em background após receber
    403/407.

    O usuário não fica esperando.
    """

    delays = [30, 60]

    try:

        logger.info(
            "[NEWS] Retry em background iniciado: source=%s limit=%s",
            cache_key[0],
            limit,
        )

        for tentativa, delay in enumerate(delays, start=1):

            await asyncio.sleep(delay)

            logger.info(
                "[NEWS] Retry background: source=%s " "tentativa=%s/%s",
                cache_key[0],
                tentativa,
                len(delays),
            )

            async with scrape_semaphore:

                try:

                    await browser_manager.start()

                    if browser_manager.context is None:
                        raise ScraperError("Contexto Playwright não foi criado")

                    items = await scraper.scrape(
                        browser_manager.context,
                        limit,
                    )

                    if items is None:
                        raise ScraperError("Scraper não retornou notícias")

                    result = NewsResponse(
                        source=scraper.source_name,
                        fetched_at=datetime.now(timezone.utc),
                        count=len(items),
                        stale=False,
                        items=list(items),
                    )

                    cache[cache_key] = (
                        asyncio.get_running_loop().time(),
                        result,
                    )

                    blocked_until.pop(cache_key, None)

                    logger.info(
                        "[NEWS] Retry background SUCESSO: " "source=%s items=%s",
                        cache_key[0],
                        len(items),
                    )

                    return

                except ScraperError as exc:

                    logger.warning(
                        "[NEWS] Retry background falhou: "
                        "source=%s tentativa=%s erro=%s",
                        cache_key[0],
                        tentativa,
                        exc,
                    )

                    continue

                except Exception as exc:

                    logger.exception(
                        "[NEWS] Erro no retry background: " "source=%s",
                        cache_key[0],
                    )

                    continue

        logger.warning(
            "[NEWS] Retry background terminou sem sucesso: " "source=%s",
            cache_key[0],
        )

    finally:

        background_refresh_tasks.pop(cache_key, None)


# ============================================================
# LIFESPAN
# ============================================================


@asynccontextmanager
async def lifespan(app: FastAPI):

    logger.info("[APP] Iniciando aplicação")

    try:
        yield

    finally:

        logger.info("[APP] Encerrando aplicação")

        await browser_manager.close()

        logger.info("[APP] Aplicação encerrada")


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=("API para coleta de notícias com Playwright."),
    lifespan=lifespan,
)


# ============================================================
# HEALTH
# ============================================================


@app.get(
    "/health",
    response_model=HealthResponse,
)
async def health() -> HealthResponse:

    return HealthResponse(
        status="ok",
        version=settings.app_version,
    )


# ============================================================
# NEWS
# ============================================================


@app.get("/news")
async def news(
    source: str = Query(
        "investing",
        min_length=1,
        max_length=30,
    ),
    limit: int = Query(
        20,
        ge=1,
        le=settings.max_news_items,
    ),
) -> NewsResponse:

    source_key = source.lower()

    # ---------------------------------------------------------
    # Verifica se a fonte existe
    # ---------------------------------------------------------

    scraper = SCRAPERS.get(source_key)

    if scraper is None:

        raise HTTPException(
            status_code=404,
            detail={
                "message": "Fonte não cadastrada",
                "available_sources": sorted(SCRAPERS),
            },
        )

    cache_key = (source_key, limit)

    # ---------------------------------------------------------
    # 1. CACHE
    # ---------------------------------------------------------

    cached = cache.get(cache_key)
    now = asyncio.get_running_loop().time()
    if cached and now - cached[0] < settings.cache_ttl_seconds:
        return cached[1]
        cached_timestamp, cached_response = cached
    cooldown_end = blocked_until.get(cache_key, 0.0)
    if now < cooldown_end:
        if cached:
            return cached[1].model_copy(update={"stale": True})
        raise HTTPException(
            status_code=429,
            detail="A fonte bloqueou temporariamente novas consultas; tente novamente depois.",
            headers={"Retry-After": str(int(cooldown_end - now) + 1)},
        )

    # ---------------------------------------------------------
    # 2. NÃO EXISTE CACHE
    # ---------------------------------------------------------

    logger.info(
        "[NEWS] Cache MISS: source=%s limit=%s",
        source_key,
        limit,
    )

    async with scrape_semaphore:

        # -----------------------------------------------------
        # Outra requisição pode ter preenchido o cache
        # enquanto esta aguardava o semaphore.
        # -----------------------------------------------------
        now = asyncio.get_running_loop().time()
        cached = cache.get(cache_key)

        if cached is not None and now - cached[0] < settings.cache_ttl_seconds:
            return cached[1]
        try:
            await browser_manager.start()
            if browser_manager.context is None:
                raise ScraperError("Contexto Playwright não foi criado")

            items = await scraper.scrape(browser_manager.context, limit)
        except ScraperError as exc:
            error_text = str(exc)

            is_blocked = (
                "HTTP 403" in error_text
                or "HTTP 407" in error_text
                or "403" in error_text
                or "407" in error_text
            )
            if is_blocked:

                blocked_until[cache_key] = asyncio.get_running_loop().time() + 60

                # ---------------------------------------------------------
                # Inicia apenas UMA tarefa de retry por fonte/limit
                # ---------------------------------------------------------

                existing_task = background_refresh_tasks.get(cache_key)

                if existing_task is None or existing_task.done():

                    task = asyncio.create_task(
                        background_retry_news(
                            cache_key=cache_key,
                            scraper=scraper,
                            limit=limit,
                        )
                    )

                    background_refresh_tasks[cache_key] = task

                    logger.info(
                        "[NEWS] Retry background agendado: " "source=%s limit=%s",
                        source_key,
                        limit,
                    )
                if cached:
                    return cached[1].model_copy(update={"stale": True})
                # ---------------------------------------------------------
                # Não espera o retry.
                # Responde imediatamente ao usuário.
                # ---------------------------------------------------------

                return JSONResponse(
                    status_code=503,
                    content={
                        "status": "processing",
                        "message": (
                            "A consulta está sendo processada. "
                            "Tente novamente em aproximadamente 1 minuto."
                        ),
                        "source": source_key,
                        "limit": limit,
                        "retry_after": 60,
                    },
                    headers={
                        "Retry-After": "60",
                    },
                )
        except Exception as exc:
            if cached:
                return cached[1].model_copy(update={"stale": True})
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if items is None:
            return None
        result = NewsResponse(
            source=scraper.source_name,
            fetched_at=datetime.now(timezone.utc),
            count=len(items),
            items=list(items),
        )
        blocked_until.pop(cache_key, None)
        cache[cache_key] = (asyncio.get_running_loop().time(), result)
        return result


# ============================================================
# SOURCES
# ============================================================


@app.get("/sources")
async def sources() -> dict[str, list[str]]:

    return {
        "sources": sorted(SCRAPERS),
    }
