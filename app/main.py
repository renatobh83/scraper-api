import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

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

# (timestamp, NewsResponse)
cache: dict[tuple[str, int], tuple[float, NewsResponse]] = {}

browser_manager = BrowserManager()

blocked_until: dict[tuple[str, int], float] = {}


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
    description="API para coleta de notícias com Playwright e cache.",
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


@app.get(
    "/news",
    response_model=NewsResponse,
)
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

    # ========================================================
    # VERIFICA CACHE
    # ========================================================

    now = asyncio.get_running_loop().time()

    cached = cache.get(cache_key)

    if cached is not None:

        cached_time, cached_result = cached

        age = now - cached_time

        # Cache ainda válido
        if age < settings.cache_ttl_seconds:

            logger.info(
                "[NEWS] Cache HIT: source=%s limit=%s age=%.1fs",
                source_key,
                limit,
                age,
            )

            return cached_result

        logger.info(
            "[NEWS] Cache expirado: source=%s limit=%s age=%.1fs",
            source_key,
            limit,
            age,
        )

    # ========================================================
    # VERIFICA COOLDOWN
    # ========================================================

    cooldown_end = blocked_until.get(
        cache_key,
        0.0,
    )

    now = asyncio.get_running_loop().time()

    if now < cooldown_end:

        # Se temos cache antigo, devolvemos ele
        if cached is not None:

            logger.warning(
                "[NEWS] Fonte em cooldown. " "Retornando cache antigo: source=%s",
                source_key,
            )

            return cached[1].model_copy(update={"stale": True})

        raise HTTPException(
            status_code=429,
            detail=("A fonte bloqueou temporariamente " "novas consultas."),
            headers={"Retry-After": str(int(cooldown_end - now) + 1)},
        )

    # ========================================================
    # UMA CONSULTA POR VEZ
    # ========================================================

    async with scrape_semaphore:

        # Outra requisição pode ter atualizado o cache
        # enquanto estávamos esperando o semaphore.

        now = asyncio.get_running_loop().time()

        cached = cache.get(cache_key)

        if cached is not None:

            cached_time, cached_result = cached

            age = now - cached_time

            if age < settings.cache_ttl_seconds:

                logger.info(
                    "[NEWS] Cache preenchido enquanto aguardava "
                    "semaphore: source=%s",
                    source_key,
                )

                return cached_result

        # ====================================================
        # INICIA BROWSER
        # ====================================================

        try:

            await browser_manager.start()

            if browser_manager.context is None:

                raise ScraperError("Contexto Playwright não foi criado")

            logger.info(
                "[NEWS] Consultando fonte: %s",
                source_key,
            )

            items = await scraper.scrape(
                browser_manager.context,
                limit,
            )

        except ScraperError as exc:

            logger.exception(
                "[NEWS] Falha ao consultar source=%s",
                source_key,
            )

            # Se temos cache, preservamos o último resultado
            cached = cache.get(cache_key)

            if cached is not None:

                logger.warning("[NEWS] Retornando cache antigo após erro")

                return cached[1].model_copy(update={"stale": True})

            # Se foi 403, ativa cooldown
            if "HTTP 403" in str(exc):

                blocked_until[cache_key] = (
                    asyncio.get_running_loop().time()
                    + settings.blocked_cooldown_seconds
                )

            raise HTTPException(
                status_code=503,
                detail=str(exc),
            ) from exc

        except Exception as exc:

            logger.exception(
                "[NEWS] Erro inesperado: source=%s",
                source_key,
            )

            cached = cache.get(cache_key)

            if cached is not None:

                return cached[1].model_copy(update={"stale": True})

            raise HTTPException(
                status_code=503,
                detail=str(exc),
            ) from exc

        # ====================================================
        # SALVA CACHE
        # ====================================================

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

        blocked_until.pop(
            cache_key,
            None,
        )

        logger.info(
            "[NEWS] Cache atualizado: source=%s limit=%s",
            source_key,
            limit,
        )

        return result


# ============================================================
# SOURCES
# ============================================================


@app.get("/sources")
async def sources() -> dict[str, list[str]]:

    return {"sources": sorted(SCRAPERS)}
