import asyncio
import logging
from datetime import datetime, timezone
from app.models import NewsResponse

logger = logging.getLogger(__name__)


async def refresh_news_cache(
    source_key: str,
    scraper,
    browser_manager,
    cache: dict,
    scrape_semaphore,
    cache_limit: int,
    max_retries: int = 3,
    retry_delay: int = 30,
):
    """
    Atualiza o cache da fonte.

    Em caso de 403, 407 ou timeout, aguarda `retry_delay`
    segundos e tenta novamente.
    """

    async with scrape_semaphore:

        for attempt in range(1, max_retries + 1):

            try:

                logger.info(
                    "[NEWS] Atualização %s - tentativa %s/%s",
                    source_key,
                    attempt,
                    max_retries,
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
                        "[NEWS] Nenhuma notícia retornada: %s",
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
                    "[NEWS] Cache atualizado: source=%s items=%s",
                    source_key,
                    len(items),
                )

                return

            except Exception as exc:

                error_text = str(exc)

                is_retryable = (
                    "HTTP 403" in error_text
                    or "HTTP 407" in error_text
                    or "Timeout" in error_text
                    or "timeout" in error_text
                )

                if not is_retryable:
                    logger.exception(
                        "[NEWS] Erro não recuperável: %s",
                        source_key,
                    )
                    return

                logger.warning(
                    "[NEWS] Falha temporária: source=%s " "tentativa=%s/%s erro=%s",
                    source_key,
                    attempt,
                    max_retries,
                    error_text,
                )

                if attempt >= max_retries:
                    logger.error(
                        "[NEWS] Todas as tentativas falharam: " "source=%s",
                        source_key,
                    )

                    # Não apagamos o cache anterior.
                    # O último resultado válido continua disponível.
                    return

                logger.info(
                    "[NEWS] Aguardando %ss antes da próxima tentativa...",
                    retry_delay,
                )

                await asyncio.sleep(retry_delay)


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
