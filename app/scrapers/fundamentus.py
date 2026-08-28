from playwright.async_api import BrowserContext, Error as PlaywrightError
from fastapi import HTTPException
from app.scrapers.base import NewsScraper, ScraperError
from playwright_stealth import Stealth
from bs4 import BeautifulSoup
import os
from app.config import settings


class FundamentusHeadlinesScraper(NewsScraper):

    source_name = "fundamentus"
    baseUrl = "https://www.fundamentus.com.br/detalhes.php?papel="

    async def scrape(
        self,
        context: BrowserContext,
        ticker: str,
    ):

        if not ticker:
            raise HTTPException(
                status_code=503,
                detail=("O Ticker deve ser informado."),
            )
        url = f"{self.baseUrl}{ticker}"
        page = await context.new_page()
        stealth = Stealth()
        await stealth.apply_stealth_async(page)
        page.set_default_timeout(settings.selector_timeout_ms)
        page.set_default_navigation_timeout(settings.navigation_timeout_ms)
        try:
            response = await page.goto(
                url,
                wait_until="commit",
                timeout=15000,
            )
            print("STATUS:", response.status if response else None)
            print("URL:", page.url)
            print("TITLE:", await page.title())
            status = response.status if response else None
            if status is not None and status >= 400:
                title = await page.title()
                raise ScraperError(
                    f"Fundamentus respondeu HTTP {status} mesmo com Playwright "
                    f"(url={page.url}, title={title!r})."
                )
            try:
                await page.wait_for_selector('table[class="w728]')
                pass
            except PlaywrightError:
                # Alguns layouts podem renderizar cards sem o atributo esperado.
                # O HTML ainda será analisado pelos seletores de fallback.
                pass
            if os.getenv("DEBUG_PAUSE", "0") == "1":
                await page.pause()
            html = await page.content()
            items = self._parse_html(html)
            print(items)
        except ScraperError:
            raise
        except PlaywrightError as exc:
            raise ScraperError(
                f"Falha no Playwright ao consultar Investing: {exc}"
            ) from exc
        finally:
            # await page.close()
            pass
        return url

    def _parse_html(self, html: str):
        dados = {}
        soup = BeautifulSoup(html, "html.parser")
        tabelas = soup.find_all("table", class_="w728")
        if len(tabelas) == 0:
            raise HTTPException(404, f"Fundo não encontrado no Fundamentus")
        for tabela in tabelas:
            linhas = tabela.find_all("tr")
            for linha in linhas:
                colunas = linha.find_all("td")
                for i in range(0, len(colunas), 2):
                    chave = colunas[i].get_text(strip=True).replace("?", "")
                    if i + 1 < len(colunas):
                        valor = colunas[i + 1].get_text(strip=True)
                        dados[chave] = valor

        return dados
