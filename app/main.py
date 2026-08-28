import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from datetime import datetime, timezone

from playwright.sync_api import ConsoleMessage
from app.browser import BrowserManager
from app.config import settings
from app.models import HealthResponse, NewsResponse
from app.scrapers import SCRAPERS
from app.scrapers.base import ScraperError
from app.scrapers.cache.news_cache import news_cache

logger = logging.getLogger(__name__)


# ============================================================
# RECURSOS GLOBAIS
# ============================================================

scrape_semaphore = asyncio.Semaphore(settings.max_concurrent_scrapes)
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

        await news_cache.clear()

        await browser_manager.close()

        logger.info("[APP] Aplicação encerrada")


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=(
        "API para coleta de notícias com Playwright " "e cache stale-while-revalidate."
    ),
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
    cached = cache.get(cache_key)

    # ---------------------------------------------------------
    # 1. CONSULTA CACHE
    # ---------------------------------------------------------

    # cached, is_fresh = news_cache.get(cache_key)

    # ---------------------------------------------------------
    # CACHE FRESCO
    # ---------------------------------------------------------

    if cached:

        logger.info(
            "[NEWS] Cache HIT: source=%s limit=%s",
            source_key,
            limit,
        )

        return cached[1]

    async with scrape_semaphore:

        # -----------------------------------------------------
        # Outra requisição pode ter preenchido o cache enquanto
        # esta aguardava o semaphore.
        # -----------------------------------------------------

        cached = cache.get(cache_key)

        if cached is not None:
            print(cached)
            return cached[1]

        # -----------------------------------------------------
        # Inicializa browser
        # -----------------------------------------------------

        try:

            await browser_manager.start()

            if browser_manager.context is None:

                raise ScraperError("Contexto Playwright não foi criado")

            # -------------------------------------------------
            # Primeira consulta.
            #
            # Aqui esperamos porque ainda não existe cache.
            # -------------------------------------------------

            items = await scraper.scrape(browser_manager.context, limit)

        except HTTPException:

            raise

        except ScraperError as exc:

            logger.exception(
                "[NEWS] Falha inicial: source=%s",
                source_key,
            )

            # -------------------------------------------------
            # Última tentativa de devolver cache.
            # -------------------------------------------------

            cached = news_cache.get(cache_key)

            if cached is not None:
                print(cached)
                return cached[1]

            raise HTTPException(
                status_code=503,
                detail=str(exc),
            ) from exc

        except Exception as exc:

            logger.exception(
                "[NEWS] Erro inesperado: source=%s",
                source_key,
            )

            # -------------------------------------------------
            # Se existir cache, não derruba a API.
            # -------------------------------------------------

            cached = news_cache.get(cache_key)

            if cached is not None:
                print(cached)
                return cached[1]

            raise HTTPException(
                status_code=503,
                detail=str(exc),
            ) from exc
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
