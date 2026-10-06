import logging
from playwright_stealth import Stealth
from playwright.async_api import Error as PlaywrightError
from app.browser import BrowserManager
from app.scrapers.base import NewsScraper, ScraperError

logger = logging.getLogger("uvicorn.error")


class BoomerangHeadlinesScraper(NewsScraper):
    source_name = "investing"
    page_url = "https://www.bloomberg.com/latest?utm_source=homepage&utm_medium=web&utm_campaign=latest"

    async def scrape(
        self,
        context: BrowserManager,
        limit: int,
    ):
        page = await context.new_page()
        logger.info("[Boomerang] Abrindo pagina")
        stealth = Stealth()

        try:
            logger.info("[Boomerang] vai chamar page.goto")
            response = await page.goto(self.page_url, wait_until="commit", timeout=3000)
            await page.pause()
            logger.info("[Boomerang] STATUS: %s", response.status if response else None)
            status = response.status if response else None
            if status is not None and status >= 400:
                title = await page.title()

                logger.warning(
                    "Investing bloqueou a navegação: status=%s url=%s title=%r",
                    status,
                    page.url,
                    title,
                )
        except ScraperError:
            raise
        except PlaywrightError as exc:
            raise ScraperError(
                f"Falha no Playwright ao consultar Investing: {exc}"
            ) from exc

        finally:
            logger.info("[Investing] Fechando pagina")
            await page.close()
