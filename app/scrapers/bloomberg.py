from datetime import datetime, timezone, timedelta
import re

import logging
from urllib.parse import urljoin
from playwright_stealth import Stealth
from playwright.async_api import Error as PlaywrightError
from app.browser import BrowserManager
from app.scrapers.base import NewsScraper, ScraperError
from bs4 import BeautifulSoup
from zoneinfo import ZoneInfo
from app.utils.models import NewsItem

logger = logging.getLogger("uvicorn.error")


class BloombergHeadlinesScraper(NewsScraper):
    source_name = "bloomberg"
    page_url = "https://www.bloomberg.com/latest?utm_source=homepage&utm_medium=web&utm_campaign=latest"

    async def scrape(
        self,
        context: BrowserManager,
        limit: int,
    ):
        page = await context.new_page()
        logger.info("[bloomberg] Abrindo pagina")
        stealth = Stealth()
        await stealth.apply_stealth_async(page)
        try:
            logger.info("[bloomberg] vai chamar page.goto")
            response = await page.goto(
                self.page_url, wait_until="domcontentloaded", timeout=30000
            )

            logger.info("[bloomberg] STATUS: %s", response.status if response else None)
            status = response.status if response else None

            if status is not None and status >= 400:
                # await page.wait_for_timeout(3000)  # 3s

                iframe_sel = 'iframe[title="Human verification challenge"]'
                iframe_el = page.locator(iframe_sel)
                iframe_style = await page.locator(
                    'iframe[title="Human verification challenge"]'
                ).get_attribute("style")
                logger.info("[Boomerang] iframe style: %r", iframe_style)

                try:
                    await iframe_el.wait_for(state="visible", timeout=20000)
                except PlaywrightError as e:
                    logger.exception(
                        "[Boomerang] iframe do captcha não ficou visível: %s", e
                    )
                    return False

                logger.info("[Boomerang] iframe visível no parent.")
                iframe_box = await iframe_el.bounding_box()
                logger.info("[Boomerang] iframe bounding box: %s", iframe_box)
                # botao = iframe_loc.locator('div[role="button"]').filter(has_text="Press & Hold")
                # n = await iframe_loc.locator('div[role="button"]').count()
                # logger.info("[Boomerang] role=button no iframe: %s", n)
                # for i in range(n):
                #     el = iframe_loc.locator('div[role="button"]').nth(i)
                #     lbl = await el.get_attribute("aria-label")
                #     txt = await el.inner_text()
                #     logger.info("[Boomerang] iframe button[%s] aria-label=%r text=%r", i, lbl, txt)

                title = await page.title()

                # await page.pause()
                try:
                    pass
                    # await botao.wait_for(state="attached", timeout=15000)
                    logger.info("[Boomerang] Botão anexado ao DOM.")

                    box = await botao.first.bounding_box()
                    logger.info("[Boomerang] Bounding box: %s", box)

                    if not box or box["width"] == 0 or box["height"] == 0:
                        logger.warning("[Boomerang] Bounding box inválido.")
                        return False

                    await page.mouse.move(
                        box["x"] + box["width"] / 2,
                        box["y"] + box["height"] / 2,
                        steps=15,
                    )
                    await page.mouse.down()
                    await page.wait_for_timeout(3000)
                    await page.mouse.up()
                except Exception as e:
                    logger.warning(f"[bloomberg] Erro ao tentar clicar no captcha: {e}")

                logger.warning(
                    "bloomberg bloqueou a navegação: status=%s url=%s title=%r",
                    status,
                    page.url,
                    title,
                )
                raise ScraperError(
                    f"bloomberg respondeu HTTP {status} mesmo com Playwright "
                    f"(url={page.url}, title={title!r})."
                )

            try:
                await page.wait_for_selector(
                    'div[class="Latest_latestBody__5ByZA"]'
                    'div[class="Latest_itemTextContainer__YMnVV"]'
                    'a[href*="/news/"]'
                    'a[class="Latest_storyLink__80QVD"]'
                    'article a[href*="/news/"]',
                    state="attached",
                )
            except:
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
            logger.info("[bloomberg] Fechando pagina")
            await page.close()

    def _parse_html(self, html: str, limit: int):
        soup = BeautifulSoup(html, "html.parser")
        items = []
        seen_urls: set[str] = set()
        breaking_news = soup.select_one('div[class="Latest_latestBody__5ByZA"]')

        if breaking_news:
            cards = breaking_news.select('div[class="Latest_itemTextContainer__YMnVV"]')
        for card in cards:
            anchor = card.select_one(
                'a[href*="/news/"], a[data-test="article-title"], ' "h2 a, h3 a"
            )
            if not anchor:
                continue
            title = " ".join(anchor.get_text(" ", strip=True).split())
            href = anchor.get("href")
            if not title or not href:
                continue
            item_url = urljoin(self.page_url, href)  # type: ignore
            if item_url in seen_urls:
                continue
            items.append(
                NewsItem(
                    title=title,
                    url=item_url,
                    source=self.source_name,
                    published_at=self._parse_datetime(card),
                    summary=None,
                    symbols=[""],
                )
            )

            seen_urls.add(item_url)
        return items

    def _parse_datetime(self, card) -> datetime | None:
        element = card.select_one(
            "time, [class='Latest_itemTimestamp__SqjF_ typography_articleLabel__G1TBw']"
        )

        if not element:
            return None

        raw_value = element.get("time") or element.get_text(" ", strip=True)

        if not raw_value:
            return None
        parsed: datetime | None = None
        # 1. Tenta ISO primeiro (mais comum e confiável)
        try:
            parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
            if not parsed.tzinfo:
                parsed = parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            # 2. Tenta formato relativo ("3 hr ago", etc.)
            parsed = self._parse_relative_time(raw_value)
        # 3. Se nada funcionou, retorna None
        if parsed is None:
            return None

        return parsed.astimezone(ZoneInfo("America/Sao_Paulo"))

    def _parse_relative_time(self, raw_value: str) -> datetime | None:
        """
        Converte strings como:
          - "3 hr ago"   -> datetime UTC
          - "5 min ago"  -> datetime UTC
          - "2 days ago" -> datetime UTC
          - "just now"   -> datetime UTC
        Retorna None se não conseguir interpretar.
        """
        if not raw_value:
            return None

        text = raw_value.strip().lower()
        now = datetime.now(timezone.utc)

        # Casos especiais
        if text in ("just now", "agora", "now"):
            return now

        # Regex para capturar número + unidade
        # Aceita: "3 hr ago", "3hr ago", "3 hours ago", "2 min ago", "10 minutes ago", "1 day ago"
        match = re.match(
            r"(\d+)\s*(sec|secs|second|seconds|min|mins|minute|minutes|"
            r"hr|hrs|hour|hours|day|days|week|weeks|month|months|year|years)\s*(ago)?",
            text,
        )
        if not match:
            return None

        value = int(match.group(1))
        unit = match.group(2)

        # Mapeamento de unidade -> timedelta
        if unit.startswith("sec"):
            delta = timedelta(seconds=value)
        elif unit.startswith("min"):
            delta = timedelta(minutes=value)
        elif unit.startswith("hr") or unit.startswith("hour"):
            delta = timedelta(hours=value)
        elif unit.startswith("day"):
            delta = timedelta(days=value)
        elif unit.startswith("week"):
            delta = timedelta(weeks=value)
        elif unit.startswith("month"):
            delta = timedelta(days=value * 30)  # aproximação
        elif unit.startswith("year"):
            delta = timedelta(days=value * 365)  # aproximação
        else:
            return None

        return now - delta
