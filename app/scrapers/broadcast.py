from datetime import datetime
import logging
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.browser import BrowserManager
from app.scrapers.base import NewsScraper, ScraperError
from playwright_stealth import Stealth
from playwright.async_api import Error as PlaywrightError

from app.utils.models import NewsItem

logger = logging.getLogger("uvicorn.error")


class BroadcastHeadlinesScraper(NewsScraper):
    source_name = "broadcast"
    page_url = "https://www.broadcast.com.br/noticias/"

    async def scrape(
        self,
        context: BrowserManager,
        limit: int,
    ):
        page = await context.new_page()
        logger.info("[broadcast] Abrindo pagina")
        stealth = Stealth()
        await stealth.apply_stealth_async(page)
        try:
            logger.info("[broadcast] vai chamar page.goto")
            response = await page.goto(
                self.page_url, wait_until="domcontentloaded", timeout=30000
            )

            logger.info("[broadcast] STATUS: %s", response.status if response else None)
            status = response.status if response else None
            try:
                await page.wait_for_selector(
                    "div.alm-listing article",
                    state="attached",
                    timeout=15000,
                )
            except PlaywrightError:
                pass
            html = await page.content()
            items = self._parse_html(html, limit)
            if not items:
                raise ScraperError(
                    "Nenhuma notícia encontrada no HTML renderizado da Investing. "
                    "Verifique os logs do container para o diagnóstico."
                )

            return items

        except PlaywrightError as exc:
            raise ScraperError(
                f"Falha no Playwright ao consultar Investing: {exc}"
            ) from exc
        except Exception as e:
            print(e)
        finally:
            logger.info("[broadcast] Fechando pagina")
            await page.close()

    def _parse_html(self, html: str, limit: int):
        soup = BeautifulSoup(html, "html.parser")
        items: list[NewsItem] = []
        seen_urls: set[str] = set()

        breaking_news = soup.select_one("div.alm-listing")

        if breaking_news:
            cards = breaking_news.select("article")

        for card in cards:
            link = card.select_one("a[href]")
            if not link:
                continue  # pula card sem link
            href = link.get("href")
            item_url = urljoin(self.page_url, href)  # type: ignore
            titulo = card.select_one("h2, h3, a")
            time_el = card.select_one("time")
            resumo = card.select_one("p.line-clamp-3")

            items.append(
                NewsItem(
                    title=titulo.get_text(strip=True) if titulo else "None",
                    url=item_url,  # type: ignore
                    source=self.source_name,
                    published_at=datetime.fromisoformat(time_el["datetime"]),  # type: ignore
                    summary=resumo.get_text(strip=True) if resumo else None,
                    symbols=[""],
                )
            )
        return items
