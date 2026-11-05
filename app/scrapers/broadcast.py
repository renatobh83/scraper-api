import logging
from datetime import datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from playwright.async_api import Error as PlaywrightError
from playwright_stealth import Stealth

from app.scrapers.base import NewsScraper, ScraperError
from app.utils.models import NewsItem

logger = logging.getLogger("uvicorn.error")


class BroadcastHeadlinesScraper(NewsScraper):
    """Scraper para notícias do portal Broadcast Agência Estado.

    Responsabilidade:
    - Acessar a página de notícias via Playwright aplicando stealth.
    - Parsear as notícias mais recentes da lista principal.
    - Fechar a página de maneira segura após a raspagem.
    """

    source_name = "broadcast"
    page_url = "https://www.broadcast.com.br/noticias/"

    async def scrape(self, page, limit: int) -> list[NewsItem]:
        logger.info("[Broadcast] Abrindo página")
        stealth = Stealth()
        await stealth.apply_stealth_async(page)

        try:
            logger.info("[Broadcast] Executando page.goto")
            response = await page.goto(
                self.page_url, wait_until="domcontentloaded", timeout=30000
            )

            status = response.status if response else None
            logger.info("[Broadcast] HTTP Status: %s", status)

            try:
                await page.wait_for_selector(
                    "div.alm-listing article",
                    state="attached",
                    timeout=15000,
                )
            except PlaywrightError:
                logger.warning(
                    "[Broadcast] Timeout aguardando artigos; tentando parsear HTML recebido"
                )

            html = await page.content()
            items = self._parse_html(html, limit)

            if not items:
                raise ScraperError("Nenhuma notícia encontrada no portal Broadcast.")

            return items

        except PlaywrightError as exc:
            raise ScraperError(
                f"Falha no Playwright ao consultar Broadcast: {exc}"
            ) from exc
        finally:
            logger.info("[Broadcast] Fechando página")
            if not page.is_closed():
                await page.close()

    def _parse_html(self, html: str, limit: int) -> list[NewsItem]:
        """Converte o documento HTML em uma lista estruturada de NewsItem."""
        soup = BeautifulSoup(html, "html.parser")
        items: list[NewsItem] = []

        breaking_news = soup.select_one("div.alm-listing")
        if not breaking_news:
            return items

        cards = breaking_news.select("article")

        for card in cards:
            link = card.select_one("a[href]")
            if not link:
                continue

            href = link.get("href")
            item_url = urljoin(self.page_url, str(href))
            titulo = card.select_one("h2, h3, a")
            time_el = card.select_one("time")
            resumo = card.select_one("p.line-clamp-3")

            published_at = None
            if time_el and time_el.get("datetime"):
                try:
                    published_at = datetime.fromisoformat(str(time_el["datetime"]))
                except ValueError:
                    published_at = None

            items.append(
                NewsItem(
                    title=titulo.get_text(strip=True) if titulo else "Sem Título",
                    url=item_url,
                    source=self.source_name,
                    published_at=published_at,
                    summary=resumo.get_text(strip=True) if resumo else None,
                    symbols=[],
                )
            )

            if len(items) >= limit:
                break

        return items
