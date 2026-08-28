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

# Cache simples:
# {
#     ("investing", 10): (
#         timestamp,
#         NewsResponse(...)
#     )
# }
cache: dict[tuple[str, int], tuple[float, NewsResponse]] = {}

browser_manager = BrowserManager()


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

    # ---------------------------------------------------------
    # 1. CACHE
    # ---------------------------------------------------------

    cached = cache.get(cache_key)

    if cached is not None:

        cached_timestamp, cached_response = cached

        logger.info(
            "[NEWS] Cache HIT: source=%s limit=%s",
            source_key,
            limit,
        )

        return cached_response

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

        cached = cache.get(cache_key)

        if cached is not None:

            cached_timestamp, cached_response = cached

            logger.info(
                "[NEWS] Cache preenchido enquanto aguardava: " "source=%s limit=%s",
                source_key,
                limit,
            )

            return cached_response

        # -----------------------------------------------------
        # 3. INICIA PLAYWRIGHT
        # -----------------------------------------------------

        try:

            await browser_manager.start()

            if browser_manager.context is None:

                raise ScraperError("Contexto Playwright não foi criado")

            # -------------------------------------------------
            # 4. SCRAPE
            # -------------------------------------------------

            logger.info(
                "[NEWS] Fazendo scrape: source=%s limit=%s",
                source_key,
                limit,
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

            # -------------------------------------------------
            # Se por algum motivo existir cache, devolve ele.
            # -------------------------------------------------

            cached = cache.get(cache_key)

            if cached is not None:

                logger.warning(
                    "[NEWS] Scrape falhou, devolvendo cache: " "source=%s",
                    source_key,
                )

                return cached[1]

            raise HTTPException(
                status_code=503,
                detail=str(exc),
            ) from exc

        except Exception as exc:

            logger.exception(
                "[NEWS] Erro inesperado source=%s",
                source_key,
            )

            cached = cache.get(cache_key)

            if cached is not None:

                return cached[1]

            raise HTTPException(
                status_code=503,
                detail=str(exc),
            ) from exc

        # -----------------------------------------------------
        # 5. VALIDA RESULTADO
        # -----------------------------------------------------

        if items is None:

            raise HTTPException(
                status_code=503,
                detail=("O scraper não retornou nenhuma notícia."),
            )

        # -----------------------------------------------------
        # 6. MONTA RESPONSE
        # -----------------------------------------------------

        result = NewsResponse(
            source=scraper.source_name,
            fetched_at=datetime.now(timezone.utc),
            count=len(items),
            stale=False,
            items=list(items),
        )

        # -----------------------------------------------------
        # 7. SALVA CACHE
        # -----------------------------------------------------

        cache[cache_key] = (
            asyncio.get_running_loop().time(),
            result,
        )

        logger.info(
            "[NEWS] Cache atualizado: source=%s limit=%s " "items=%s",
            source_key,
            limit,
            len(items),
        )

        # -----------------------------------------------------
        # 8. RETORNA
        # -----------------------------------------------------

        return result


# ============================================================
# SOURCES
# ============================================================


@app.get("/sources")
async def sources() -> dict[str, list[str]]:

    return {
        "sources": sorted(SCRAPERS),
    }
