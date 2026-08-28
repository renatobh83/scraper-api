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
    historico = "https://www.fundamentus.com.br/amline/cot_hist.php?papel="

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

        page = await context.new_page()
        stealth = Stealth()
        await stealth.apply_stealth_async(page)
        page.set_default_timeout(settings.selector_timeout_ms)
        page.set_default_navigation_timeout(settings.navigation_timeout_ms)
        try:
            url_detalhes = f"{self.baseUrl}{ticker}"
            response_detalhes = await page.goto(
                url_detalhes,
                wait_until="commit",
                timeout=15000,
            )
            print("STATUS:", response_detalhes.status if response_detalhes else None)
            print("URL:", page.url)
            print("TITLE:", await page.title())

            status = response_detalhes.status if response_detalhes else None
            if status is not None and status >= 400:
                title = await page.title()
                raise ScraperError(
                    f"Fundamentus respondeu HTTP {status} mesmo com Playwright "
                    f"(url={page.url}, title={title!r})."
                )
            try:
                await page.wait_for_selector('table[class="w728]', timeout=5000)
            except PlaywrightError:
                # Alguns layouts podem renderizar cards sem o atributo esperado.
                # O HTML ainda será analisado pelos seletores de fallback.
                pass

            html = await page.content()
            dados_detalhes = self._parse_html(html)
            # print("DADOS DETALHES:", dados_detalhes)

            # Segunda URL - histórico
            url_historico_api = f"{self.historico}{ticker}"
            await page.set_extra_http_headers(
                {
                    "Referer": f"https://www.fundamentus.com.br/detalhes.php?papel={ticker}",  # REFERER CRUCIAL
                }
            )
            response_historico = await page.goto(
                url_historico_api,
                wait_until="commit",
                timeout=15000,
            )
            print(
                "STATUS HISTORICO:",
                response_historico.status if response_historico else None,
            )

            if os.getenv("DEBUG_PAUSE", "0") == "1":
                await page.pause()
                # Para requisições AJAX, o conteúdo pode ser JSON
            if response_historico:
                # Tenta pegar o JSON diretamente
                try:
                    dados_historico = await response_historico.json()
                    print("DADOS HISTÓRICOS (JSON):", dados_historico)
                except:
                    # Se não for JSON, pega o texto
                    html2 = await page.content()
                    dados_historico = self._parse_html_historico(html2)

            return {"detalhes": dados_detalhes, "historico": dados_historico}
        except ScraperError:
            raise
        except PlaywrightError as exc:
            raise ScraperError(
                f"Falha no Playwright ao consultar Investing: {exc}"
            ) from exc
        finally:
            await page.close()
            pass

    def parse_cookie_string(self, cookie_string: str):
        """Converte string de cookies para lista de dicionários do Playwright"""
        cookies = []
        for item in cookie_string.split(";"):
            item = item.strip()
            if not item:
                continue
            if "=" in item:
                name, value = item.split("=", 1)
                cookies.append(
                    {
                        "name": name.strip(),
                        "value": value.strip(),
                        "domain": ".fundamentus.com.br",  # Ajuste conforme necessário
                        "path": "/",
                        "httpOnly": False,
                        "secure": False,
                        "sameSite": "Lax",
                    }
                )
        return cookies

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
