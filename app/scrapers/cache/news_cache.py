import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable, Awaitable
from app.models import NewsItem, NewsResponse

logger = logging.getLogger(__name__)


async def refresh_news_cache(
    source_key: str,
    scraper,
    browser_manager,
    cache: dict,
    scrape_semaphore,
    cache_limit: int,
):
    """
    Consulta a fonte e atualiza o cache.

    Esta função não conhece o FastAPI nem importa app.main.
    """

    async with scrape_semaphore:

        try:
            logger.info(
                "Iniciando atualização do cache: source=%s",
                source_key,
            )

            await browser_manager.start()

            if browser_manager.context is None:
                raise RuntimeError("Contexto Playwright não foi criado")

            items = await scraper.scrape(
                browser_manager.context,
                cache_limit,
            )

            if not items:
                logger.warning(
                    "Nenhuma notícia retornada para %s",
                    source_key,
                )
                return

            result = NewsResponse(
                source=scraper.source_name,
                fetched_at=datetime.now(timezone.utc),
                count=len(items),
                items=list(items),
            )

            cache[source_key] = (
                asyncio.get_running_loop().time(),
                result,
            )

            logger.info(
                "Cache atualizado: source=%s items=%s",
                source_key,
                len(items),
            )

        except Exception:
            logger.exception(
                "Erro atualizando cache da fonte %s",
                source_key,
            )


async def news_cache_scheduler(
    scrapers,
    browser_manager,
    cache,
    scrape_semaphore,
    cache_limit: int,
    interval: int = 60,
):
    """
    Atualiza as fontes automaticamente a cada `interval` segundos.
    """
    logger.info(
        "News cache scheduler iniciado: intervalo=%ss",
        interval,
    )

    while True:

        try:

            for source_key, scraper in scrapers.items():

                await refresh_news_cache(
                    source_key=source_key,
                    scraper=scraper,
                    browser_manager=browser_manager,
                    cache=cache,
                    scrape_semaphore=scrape_semaphore,
                    cache_limit=cache_limit,
                )

            await asyncio.sleep(interval)

        except asyncio.CancelledError:

            logger.info("News cache scheduler finalizado")

            raise

        except Exception:

            logger.exception("Erro no scheduler de notícias")

            await asyncio.sleep(10)
