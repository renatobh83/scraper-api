import asyncio
import json
import logging
import os
import random
import re
from datetime import datetime, timezone
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from playwright.async_api import Error as PlaywrightError

from app.scrapers.base import NewsScraper, ScraperError
from app.utils.models import NewsItem

logger = logging.getLogger("uvicorn.error")


class InvestingHeadlinesScraper(NewsScraper):
    """Scraper para o portal Investing Brasil usando Playwright com bypassing de Cloudflare Turnstile.

    Responsabilidade:
    - Acessar a página de notícias da Investing.
    - Resolver o desafio do Cloudflare Turnstile simulando cliques do usuário.
    - Parsear o HTML extraindo títulos, URLs, datas e tickers de ações/commodities.
    - Garantir o fechamento da página no bloco finally.
    """

    source_name = "investing"
    page_url = "https://br.investing.com/news/headlines"

    async def scrape(self, page, limit: int) -> list[NewsItem]:
        """Executa a raspagem de notícias dentro de uma página pré-existente."""
        logger.info("[Investing] Abrindo página")
        try:
            await page.goto(self.page_url, wait_until="domcontentloaded")

            # Resolve validação do Cloudflare caso apareça
            if not await self.resolver_cloudflare(page):
                await self._save_debug_artifacts(page, "turnstile_falhou")
                raise ScraperError("Turnstile não resolvido (desafio persistente).")

            try:
                await page.wait_for_selector(
                    'div[data-test="breaking-news"] '
                    'div[data-test="breaking-news-article"] '
                    'a[href*="/news/"], '
                    'a[data-test="article-title"], '
                    'article a[href*="/news/"]',
                    state="attached",
                    timeout=15000,
                )
            except PlaywrightError:
                logger.warning(
                    "[Investing] Timeout aguardando seletor principal; tentando parsear com seletores de fallback"
                )

            html = await page.content()
            items = self._parse_html(html, limit)

            if not items:
                raise ScraperError("Nenhuma notícia encontrada no HTML da Investing.")

            return items

        except PlaywrightError as exc:
            raise ScraperError(
                f"Falha no Playwright ao consultar Investing: {exc}"
            ) from exc
        finally:
            logger.info("[Investing] Fechando página de raspagem")
            if not page.is_closed():
                await page.close()

    async def _desafio_ativo(self, page) -> bool:
        """Verifica se a página atual ainda é o desafio do Cloudflare."""
        try:
            title = await page.title()
        except PlaywrightError:
            return True
        return "Um momento" in title or "Just a moment" in title

    async def resolver_cloudflare(self, page, tentativas: int = 3) -> bool:
        """Tenta detectar e interagir com o desafio Cloudflare Turnstile."""
        for n in range(1, tentativas + 1):
            if not await self._desafio_ativo(page):
                return True

            logger.info("[Investing] Turnstile tentativa %s/%s", n, tentativas)
            await page.wait_for_timeout(random.randint(2500, 4000))

            if not await self.clicar_turnstile(page, timeout=15):
                continue

            try:
                await page.wait_for_function(
                    "!document.title.includes('Um momento') && "
                    "!document.title.includes('Just a moment')",
                    timeout=20000,
                )
                return True
            except PlaywrightError:
                logger.warning("[Investing] Desafio reapareceu, tentando novamente")

        return not await self._desafio_ativo(page)

    async def clicar_turnstile(self, page, timeout=30) -> bool:
        """Localiza o iframe do Turnstile e efetua o movimento humano até o checkbox."""
        for _ in range(timeout * 2):
            for frame in page.frames:
                if "challenges.cloudflare.com" in frame.url:
                    try:
                        el = await frame.frame_element()
                        box = await el.bounding_box()
                    except Exception:
                        box = None

                    if box and box["width"] > 0:
                        x = box["x"] + 30 + random.uniform(-3, 3)
                        y = box["y"] + box["height"] / 2 + random.uniform(-3, 3)

                        await page.mouse.move(x - 120, y - 40, steps=15)
                        await page.mouse.move(x, y, steps=20)
                        await asyncio.sleep(random.uniform(0.2, 0.5))
                        await page.mouse.click(x, y)
                        return True
            await asyncio.sleep(0.5)
        return False

    async def _save_debug_artifacts(self, page, reason: str) -> None:
        """Salva screenshots e HTML para auxílio em diagnóstico em caso de erros."""
        if os.getenv("SAVE_DEBUG_ARTIFACTS", "1") != "1":
            return

        directory = os.getenv("DEBUG_ARTIFACT_DIR", "/tmp/news-debug")
        os.makedirs(directory, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        prefix = os.path.join(directory, f"investing_{stamp}_{reason}")

        try:
            await page.screenshot(path=f"{prefix}.png", full_page=True)
            html = await page.content()
            with open(f"{prefix}.html", "w", encoding="utf-8") as f:
                f.write(html)

            metadata = {"url": page.url, "title": await page.title(), "reason": reason}
            with open(f"{prefix}.json", "w", encoding="utf-8") as f:
                json.dump(metadata, f, ensure_ascii=False, indent=2)

            logger.warning("Artefatos de diagnóstico salvos em %s.*", prefix)
        except Exception as exc:
            logger.warning("Erro salvando artefatos de diagnóstico: %s", exc)

    def _parse_html(self, html: str, limit: int) -> list[NewsItem]:
        """Extrai a estrutura de artigos a partir do texto HTML retornado."""
        soup = BeautifulSoup(html, "html.parser")
        items: list[NewsItem] = []
        seen_urls: set[str] = set()

        breaking_news = soup.select_one('div[data-test="breaking-news"]')
        cards = (
            breaking_news.select('div[data-test="breaking-news-article"]')
            if breaking_news
            else soup.select('article, [data-test="article-list-item"], .articleItem')
        )

        for card in cards:
            anchor = card.select_one(
                'a[href*="/news/"], a[data-test="article-title"], h2 a, h3 a'
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
        """Identifica códigos de ativos (ex: PETR4, VALE3) citados na notícia."""
        symbols: list[str] = []
        seen_symbols: set[str] = set()
        asset_href_selector = (
            'a[href*="/equities/"], a[href*="/commodities/"], '
            'a[href*="/currencies/"], a[href*="/indices/"], a[href*="/etfs/"]'
        )

        related_links = card.select(
            "div.news-headlines-list-item.related-pairs " + asset_href_selector
        )
        if not related_links:
            related_links = card.select(asset_href_selector)

        for link in related_links:
            symbol = InvestingHeadlinesScraper._symbol_from_link(link)
            if symbol and symbol not in seen_symbols:
                symbols.append(symbol)
                seen_symbols.add(symbol)

        return symbols

    @staticmethod
    def _symbol_from_link(link) -> str | None:
        """Filtra o texto do elemento procurando padrões válidos de tickers de mercado."""
        candidates = link.select("span[title], span")
        candidates.append(link)

        for element in candidates:
            text = " ".join(element.get_text(" ", strip=True).split()).upper()
            if re.fullmatch(r"[A-Z]{1,8}(?:[.\-][A-Z0-9]{1,4})?\d{0,2}", text):
                return text

            tokens = re.findall(r"\b[A-Z]{1,8}(?:[.\-][A-Z0-9]{1,4})?\d{0,2}\b", text)
            if tokens:
                return tokens[-1]

        return None

    @staticmethod
    def _parse_datetime(card) -> datetime | None:
        """Converte a data de publicação extraída da tag time para objeto datetime UTC."""
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
        """Extrai o resumo da notícia se disponível no card."""
        element = card.select_one(
            '[data-test="article-description"], p, .articleItem__summary'
        )
        if not element:
            return None
        summary = " ".join(element.get_text(" ", strip=True).split())
        return summary or None
