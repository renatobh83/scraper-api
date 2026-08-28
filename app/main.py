import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query

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

    # ---------------------------------------------------------
    # 1. CONSULTA CACHE
    # ---------------------------------------------------------

    cached, is_fresh = news_cache.get(cache_key)

    # ---------------------------------------------------------
    # CACHE FRESCO
    # ---------------------------------------------------------

    if cached is not None and is_fresh:

        logger.info(
            "[NEWS] Cache HIT: source=%s limit=%s",
            source_key,
            limit,
        )

        return cached

    # ---------------------------------------------------------
    # CACHE STALE
    # ---------------------------------------------------------

    if cached is not None:

        logger.info(
            "[NEWS] Cache STALE: source=%s limit=%s",
            source_key,
            limit,
        )

        try:

            await browser_manager.start()

            if browser_manager.context is not None:

                news_cache.start_background_refresh(
                    key=cache_key,
                    scraper=scraper,
                    context=browser_manager.context,
                    limit=limit,
                )

        except Exception:

            logger.exception(
                "[NEWS] Não foi possível iniciar " "refresh em background: source=%s",
                source_key,
            )

        # -----------------------------------------------------
        # IMPORTANTE:
        #
        # Não espera o Investing.
        #
        # Retorna imediatamente o cache antigo.
        # -----------------------------------------------------

        return cached

    # ---------------------------------------------------------
    # 2. CACHE MISS
    #
    # Não temos nenhum resultado armazenado.
    #
    # Nesse caso precisamos consultar a fonte.
    # ---------------------------------------------------------

    logger.info(
        "[NEWS] Cache MISS: source=%s limit=%s",
        source_key,
        limit,
    )

    async with scrape_semaphore:

        # -----------------------------------------------------
        # Outra requisição pode ter preenchido o cache enquanto
        # esta aguardava o semaphore.
        # -----------------------------------------------------

        cached, is_fresh = news_cache.get(cache_key)

        if cached is not None:

            return cached

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

            result = await news_cache.refresh(
                key=cache_key,
                scraper=scraper,
                context=browser_manager.context,
                limit=limit,
            )

            # -------------------------------------------------
            # Se falhou e não existe cache
            # -------------------------------------------------

            if result is None:

                raise HTTPException(
                    status_code=503,
                    detail=(
                        "Não foi possível obter notícias "
                        "e não existe cache disponível."
                    ),
                )

            return result

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

            cached, _ = news_cache.get(cache_key)

            if cached is not None:

                return cached

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

            cached, _ = news_cache.get(cache_key)

            if cached is not None:

                return cached

            raise HTTPException(
                status_code=503,
                detail=str(exc),
            ) from exc


# ============================================================
# SOURCES
# ============================================================


@app.get("/sources")
async def sources() -> dict[str, list[str]]:

    return {
        "sources": sorted(SCRAPERS),
    }
