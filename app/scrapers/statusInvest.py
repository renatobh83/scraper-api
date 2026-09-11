import os
from fastapi import HTTPException
from playwright.async_api import BrowserContext
from playwright_stealth import Stealth
from app.scrapers.base import NewsScraper
from bs4 import BeautifulSoup
from app.config import settings


class StatusInvestHeadlinesScraper(NewsScraper):
    source_name = "statusinvest"
    base_url = "https://statusinvest.com.br/acoes/"

    async def scrape(
        self,
        context: BrowserContext,
        ticker: str,
    ):
        page = await context.new_page()
        stealth = Stealth()
        await stealth.apply_stealth_async(page)
        page.set_default_timeout(settings.selector_timeout_ms)
        page.set_default_navigation_timeout(settings.navigation_timeout_ms)
        url_search = f"{self.base_url}{ticker}"
        response = await page.goto(
            "https://statusinvest.com.br/acoes/mglu3",
            wait_until="commit",
            timeout=15000,
        )
        await page.set_extra_http_headers(
            {
                "Origin": "https://statusinvest.com.br",
                "Referer": f"https://statusinvest.com.br/acoes/{ticker.lower()}",
                "sec-fetch-mode": "cors",
            }
        )
        print(
            "STATUS HISTORICO:",
            response.status if response else None,
        )
        if os.getenv("DEBUG_PAUSE", "0") == "1":
            await page.pause()
        if response:
            try:
                dados = await response.json()
                print(dados)
            except:
                html = await page.content()
                # dados = self._parse_html(html)

    def _parse_html(self, html: str):
        dados = {}
        soup = BeautifulSoup(html, "html.parser")
        div_principal = soup.find("div", class_="has-special")

        if div_principal is None:
            raise HTTPException(404, f"Elemento não encontrado")
        # Pegar todos os filhos diretos (apenas divs filhas)
        filhos_div = div_principal.find_all("div", recursive=False)

        # # Pegar todas as divs dentro (incluindo aninhadas)
        todos_filhos = div_principal.find_all("div")

        # # Pegar apenas os filhos diretos com classe específica
        filhos_com_classe = div_principal.find_all(
            "div", class_="info", recursive=False
        )

        print("filhos_div", len(filhos_div))
        print("todos_filhos", len(todos_filhos))
        print("filhos_com_classe", len(filhos_com_classe))
        # if len(tabelas) == 0:
        #     raise HTTPException(404, f"Fundo não encontrado no Fundamentus")
        # for tabela in tabelas:
        #     linhas = tabela.find_all("tr")
        #     for linha in linhas:
        #         colunas = linha.find_all("td")
        #         for i in range(0, len(colunas), 2):
        #             chave = colunas[i].get_text(strip=True).replace("?", "")
        #             if i + 1 < len(colunas):
        #                 valor = colunas[i + 1].get_text(strip=True)
        #                 dados[chave] = valor
        # return dados
