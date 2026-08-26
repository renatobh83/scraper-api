from datetime import datetime, timezone
import os
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from playwright.async_api import BrowserContext, Error as PlaywrightError

from app.config import settings
from app.models import NewsItem
from app.scrapers.base import NewsScraper, ScraperError


class InvestingHeadlinesScraper(NewsScraper):
    source_name = "investing"
    page_url = "https://br.investing.com/news/headlines"

    async def scrape(
        self,
        context: BrowserContext,
        limit: int,
    ) -> list[NewsItem]:
        page = await context.new_page()
        page.set_default_timeout(settings.selector_timeout_ms)
        page.set_default_navigation_timeout(settings.navigation_timeout_ms)

        try:
            response = await page.goto(
                self.page_url,
                wait_until="domcontentloaded",
                timeout=settings.navigation_timeout_ms,
            )

            status = response.status if response else None
            if status is not None and status >= 400:
                raise ScraperError(
                    f"Investing respondeu HTTP {status} mesmo com Playwright."
                )

            # O seletor final pode ser ajustado após inspecionar a estrutura real
            # que a Investing entrega para o seu IP/região.
            try:
                await page.wait_for_selector(
                    'div[data-test="breaking-news"] '
                    'div[data-test="breaking-news-article"] '
                    'a[href*="/news/"], '
                    'a[data-test="article-title"], '
                    'article a[href*="/news/"]',
                    state="attached",
                    timeout=settings.selector_timeout_ms,
                )
            except PlaywrightError:
                # Alguns layouts podem renderizar cards sem o atributo esperado.
                # O HTML ainda será analisado pelos seletores de fallback.
                pass

            if os.getenv("DEBUG_PAUSE", "0") == "1":
                await page.pause()

            html = await page.content()
            return self._parse_html(html, limit)
        except ScraperError:
            raise
        except PlaywrightError as exc:
            raise ScraperError(
                f"Falha no Playwright ao consultar Investing: {exc}"
            ) from exc
        finally:
            await page.close()

    def _parse_html(self, html: str, limit: int) -> list[NewsItem]:
        soup = BeautifulSoup(html, "html.parser")
        items: list[NewsItem] = []
        seen_urls: set[str] = set()

        # Estrutura observada no DOM compartilhado pelo usuário:
        # div[data-test="breaking-news"]
        #   div[data-test="breaking-news-article"]
        #     a[href="/news/..."]
        breaking_news = soup.select_one('div[data-test="breaking-news"]')
        if breaking_news:
            cards = breaking_news.select('div[data-test="breaking-news-article"]')
        else:
            # Fallback para versões/layouts diferentes da página.
            cards = soup.select(
                'article, [data-test="article-list-item"], '
                '[data-test="article-item"], .articleItem'
            )

        for card in cards:
            # No layout atual, o título está no primeiro link de notícia
            # dentro de cada breaking-news-article.
            anchor = card.select_one(
                'a[href*="/news/"], a[data-test="article-title"], ' "h2 a, h3 a"
            )
            if not anchor:
                continue

            title = " ".join(anchor.get_text(" ", strip=True).split())
            href = anchor.get("href")
            if not title or not href:
                continue

            item_url = urljoin(self.page_url, href)
            if item_url in seen_urls:
                continue

            items.append(
                NewsItem(
                    title=title,
                    url=item_url,
                    source=self.source_name,
                    published_at=self._parse_datetime(card),
                    summary=self._extract_summary(card),
                    symbols=self._extract_symbols(card),
                )
            )
            seen_urls.add(item_url)

            if len(items) >= limit:
                break

        return items

    @staticmethod
    def _extract_symbols(card) -> list[str]:
        """Extrai símbolos dos ativos relacionados à notícia.

        Estrutura observada no DOM:
        .news-headlines-list-item.related-pairs
          a[href*="/equities/"], a[href*="/commodities/"]
            span[title="Nome da empresa"].text -> símbolo

        O seletor de URL inclui outros tipos de ativos porque a Investing
        pode relacionar ações, commodities, índices, moedas e ETFs.
        """
        symbols: list[str] = []
        seen_symbols: set[str] = set()
        asset_href_selector = (
            'a[href*="/equities/"], '
            'a[href*="/commodities/"], '
            'a[href*="/currencies/"], '
            'a[href*="/indices/"], '
            'a[href*="/etfs/"], '
            'a[href*="/funds/"]'
        )

        related_links = card.select(
            "div.news-headlines-list-item.related-pairs " + asset_href_selector
        )
        if not related_links:
            # Fallback caso as classes utilitárias mudem, mas o bloco
            # related-pairs continue identificado no HTML.
            related_links = card.select(
                '[class*="related-pairs"] ' + asset_href_selector
            )
        if not related_links:
            related_links = card.select(asset_href_selector)

        for link in related_links:
            symbol = InvestingHeadlinesScraper._symbol_from_link(link)
            if not symbol or symbol in seen_symbols:
                continue

            symbols.append(symbol)
            seen_symbols.add(symbol)

        return symbols

    @staticmethod
    def _symbol_from_link(link) -> str | None:
        """Obtém o símbolo sem confundir o nome da empresa com o ativo."""
        # No layout mostrado, o símbolo é o texto do span que possui title
        # com o nome completo da empresa.
        candidates = link.select("span[title], span")
        candidates.append(link)

        for element in candidates:
            text = " ".join(element.get_text(" ", strip=True).split()).upper()
            if re.fullmatch(r"[A-Z]{1,8}(?:[.\-][A-Z0-9]{1,4})?\d{0,2}", text):
                return text

            # Se houver texto complementar no link, procure um token com
            # aparência de símbolo, como LMT, LCO, PETRA4 ou AAPL.
            tokens = re.findall(
                r"\b[A-Z]{1,8}(?:[.\-][A-Z0-9]{1,4})?\d{0,2}\b",
                text,
            )
            if tokens:
                return tokens[-1]

        return None

    @staticmethod
    def _parse_datetime(card) -> datetime | None:
        element = card.select_one("time[datetime], [data-test='article-publish-date']")
        if not element:
            return None

        raw_value = element.get("datetime") or element.get_text(" ", strip=True)
        if not raw_value:
            return None

        try:
            parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    @staticmethod
    def _extract_summary(card) -> str | None:
        element = card.select_one(
            '[data-test="article-description"], p, .articleItem__summary'
        )
        if not element:
            return None

        summary = " ".join(element.get_text(" ", strip=True).split())
        return summary or None
