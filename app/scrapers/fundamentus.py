import asyncio
import os
import json
import re
from datetime import datetime
from typing import List, Dict, Any
from playwright.async_api import BrowserContext, Error as PlaywrightError
from fastapi import HTTPException
from app.scrapers.base import NewsScraper, ScraperError
from playwright_stealth import Stealth
from bs4 import BeautifulSoup
from app.config import settings
from calendar import monthrange
from app.scrapers.adapters.Fundamentus.fundamentus_type import (
    build_response,
)

from typing import Any, Optional


class FundamentusHeadlinesScraper(NewsScraper):

    source_name = "fundamentus"
    baseUrl = "https://www.fundamentus.com.br/detalhes.php?papel="
    historico = "https://www.fundamentus.com.br/amline/cot_hist.php?papel="
    proventos = "https://www.fundamentus.com.br/fii_proventos.php?papel="

    async def search(
        self,
        context: BrowserContext,
        ticker: str,
    ):
        if not ticker:
            raise HTTPException(
                status_code=503,
                detail=("O Ticker deve ser informado."),
            )

        page = await self._openPage(context)
        try:

            # url_historico_proventos = f"{self.proventos}{ticker}&tipo=2"

            # response_detalhes = await page.goto(
            #     url_historico_proventos,
            #     wait_until="domcontentloaded",
            #     timeout=15000,
            # )
            # if os.getenv("DEBUG_PAUSE", "0") == "1":
            #     await page.pause()
            # status = response_detalhes.status if response_detalhes else None
            # if status is not None and status >= 400:
            #     title = await page.title()
            #     raise ScraperError(
            #         f"Fundamentus respondeu HTTP {status} mesmo com Playwright "
            #         f"(url={page.url}, title={title!r})."
            #     )
            # try:
            #     await page.wait_for_selector('table[id="resultado]', timeout=5000)

            # except PlaywrightError:
            #     # Alguns layouts podem renderizar cards sem o atributo esperado.
            #     # O HTML ainda será analisado pelos seletores de fallback.
            #     pass
            # html = await page.content()
            # proventos = self._parse_proventos_html(html)
            url_detalhes = f"{self.baseUrl}{ticker}"

            response_detalhes = await page.goto(
                url_detalhes,
                wait_until="domcontentloaded",
                timeout=15000,
            )
            print("STATUS:", response_detalhes.status if response_detalhes else None)
            status = response_detalhes.status if response_detalhes else None
            if status is not None and status >= 400:
                title = await page.title()
                raise ScraperError(
                    f"Fundamentus respondeu HTTP {status} mesmo com Playwright "
                    f"(url={page.url}, title={title!r})."
                )

            await page.wait_for_timeout(1000)
            html = await page.content()
            # Verificar se existe a tabela de detalhes (ativo único)
            has_detalhes = await page.query_selector('table[class="w728"]')

            # Verificar se existe a tabela de lista (múltiplos ativos)
            has_lista = await page.query_selector('table[id="test1"]')

            has_any_table = await page.query_selector("table")
            # Se tiver a tabela de detalhes (ativo único)
            if has_detalhes:
                print("Processando como ativo único...")
                try:
                    await page.wait_for_selector('table[class="w728"]', timeout=5000)
                except PlaywrightError:
                    pass
                html = await page.content()
                dados_detalhes = self._parse_html(html)
                if self._is_fii(dados_detalhes):
                    print(f"🔍 Buscando proventos para FII: {ticker}")
                    proventos = await self._buscar_proventos(context, ticker)
                    if proventos:
                        dados_detalhes["proventos"] = proventos
                    else:
                        dados_detalhes["proventos"] = None
                else:
                    dados_detalhes["proventos"] = None

                return build_response([dados_detalhes])
            elif has_lista:
                print("Processando como lista de ativos...")
                try:
                    await page.wait_for_selector('table[id="test1"]', timeout=5000)
                except PlaywrightError:
                    pass
                html = await page.content()
                # Aqui você precisa de um método diferente para parsear a lista
                dados_lista = self._parse_list_html(html)  # <- await que faltava
                resultados_filtrados = [
                    item
                    for item in dados_lista
                    if (
                        ticker.lower() in item.get("ticker", "").lower()
                        or ticker.lower() in item.get("nome", "").lower()
                        or ticker.lower() in item.get("razao_social", "").lower()
                    )
                ]
                # Ordena: primeiro os que começam com o termo, depois os que contêm
                resultados_filtrados.sort(
                    key=lambda x: (
                        (
                            0
                            if x.get("ticker", "").lower().startswith(ticker.lower())
                            else 1
                        ),
                        x.get("ticker", "").lower(),
                    )
                )
                tickers_encontrados = [item["ticker"] for item in resultados_filtrados]

                detalhes_completos = await self._buscar_detalhes_da_lista(
                    context, tickers_encontrados
                )
                # 🔥 PARA CADA FII NA LISTA, BUSCA PROVENTOS
                for i, item in enumerate(detalhes_completos):
                    print(i)
                    if self._is_fii(item):
                        print(f"🔍 Buscando proventos para FII: {item.get('ticker')}")
                        proventos = await self._buscar_proventos(
                            context, item.get("ticker")
                        )
                        if proventos:
                            detalhes_completos[i]["proventos"] = proventos
                        else:
                            detalhes_completos[i]["proventos"] = None
                    else:
                        detalhes_completos[i]["proventos"] = None
                return build_response(detalhes_completos)
        except ScraperError:
            raise
        except PlaywrightError as exc:
            raise ScraperError(
                f"Falha no Playwright ao consultar Investing: {exc}"
            ) from exc
        finally:
            await page.close()
            pass

    async def scrape(
        self,
        context: BrowserContext,
        ticker: str,
        interval: str,
        order: bool,
    ):

        if not ticker:
            raise HTTPException(
                status_code=503,
                detail=("O Ticker deve ser informado."),
            )

        page = await self._openPage(context)
        try:
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

                    historico = self.processar_historico_fundamentus(
                        dados_historico, periodo=interval, sortOrder=order
                    )

                except:
                    # Se não for JSON, pega o texto
                    html2 = await page.content()
                    dados_historico = self.processar_historico_fundamentus(html2)

            retorno = {
                "requestedSymbol": ticker,
                "symbol": ticker,
                "results": [
                    {
                        "data": {
                            "usedInterval": "1d",
                            "usedRange": "1mo",
                            "historicalDataPrice": historico,
                        }
                    }
                ],
            }
            return retorno
        except ScraperError:
            raise
        except PlaywrightError as exc:
            raise ScraperError(
                f"Falha no Playwright ao consultar Investing: {exc}"
            ) from exc
        finally:
            await page.close()
            pass

    async def _fetch_detalhes(
        self, context: BrowserContext, ticker: str
    ) -> Dict[str, Any]:
        """
        Abre uma página nova, navega até a página de detalhes de UM ticker
        e retorna o dicionário já parseado (sem o wrapper de build_response).
        Reaproveitado tanto pelo fluxo de ticker único quanto pelo enriquecimento da lista.
        """
        page = await self._openPage(context)
        try:
            url_detalhes = f"{self.baseUrl}{ticker}"
            response = await page.goto(url_detalhes, wait_until="commit", timeout=15000)
            status = response.status if response else None
            if status is not None and status >= 400:
                raise ScraperError(
                    f"Fundamentus respondeu HTTP {status} para o ticker {ticker}"
                )
            await page.wait_for_timeout(1000)
            try:
                await page.wait_for_selector('table[class="w728"]', timeout=5000)
            except PlaywrightError:
                pass
            html = await page.content()
            return self._parse_html(html)
        finally:
            await page.close()

    async def _buscar_proventos(
        self, context: BrowserContext, ticker: str
    ) -> Optional[dict]:
        """
        Busca os proventos de um FII usando Playwright.
        Retorna None se não for FII ou se não encontrar dados.
        """
        try:
            url_proventos = f"{self.proventos}{ticker}&tipo=2"

            # Cria uma nova página para buscar proventos
            page_proventos = await context.new_page()

            try:
                await page_proventos.goto(
                    url_proventos, wait_until="domcontentloaded", timeout=15000
                )

                # Aguarda a tabela carregar
                try:
                    await page_proventos.wait_for_selector(
                        'table[id="resultado"]', timeout=5000
                    )
                except PlaywrightError:
                    # Se não encontrar a tabela, pode ser que não tenha proventos
                    return None

                # Extrai os dados da tabela usando JavaScript
                proventos = await page_proventos.evaluate("""
                    () => {
                        const rows = document.querySelectorAll('#resultado tr');
                        const result = [];

                        // Pula o cabeçalho (primeira linha)
                        for (let i = 1; i < Math.min(rows.length, 3); i++) {
                            const cols = rows[i].querySelectorAll('td');
                            if (cols.length >= 4) {
                                const valor = cols[3].textContent.trim().replace(',', '.');
                                result.push({
                                    competencia: cols[0].textContent.trim(),
                                    dataPagamento: cols[2].textContent.trim(),
                                    valor: parseFloat(valor)
                                });
                            }
                        }
                        return result;
                    }
                """)

                if proventos and len(proventos) > 0:
                    return proventos[0]

                return None

            finally:
                await page_proventos.close()

        except Exception as e:
            print(f"Erro ao buscar proventos para {ticker}: {e}")
            return None

    async def _buscar_detalhes_da_lista(
        self, context: BrowserContext, tickers: List[str], max_concorrentes: int = 3
    ) -> List[Dict[str, Any]]:
        """
        Para cada ticker encontrado na tabela de lista, busca os dados completos
        (página de detalhes), com um limite de concorrência para não sobrecarregar
        o Fundamentus nem seu proxy.
        """
        semaforo = asyncio.Semaphore(max_concorrentes)
        resultados: List[Dict[str, Any]] = []

        async def _buscar_um(tk: str):
            async with semaforo:
                try:
                    dados = await self._fetch_detalhes(context, tk)
                    resultados.append(dados)
                except (ScraperError, PlaywrightError) as exc:
                    print(f"Falha ao buscar detalhes de {tk}: {exc}")

        await asyncio.gather(*(_buscar_um(tk) for tk in tickers))
        return resultados

    async def _openPage(self, context):
        page = await context.new_page()
        stealth = Stealth()
        await stealth.apply_stealth_async(page)
        page.set_default_timeout(settings.selector_timeout_ms)
        page.set_default_navigation_timeout(settings.navigation_timeout_ms)
        return page

    def extrair_json_do_html(self, conteudo: str) -> List[List]:
        """
        Extrai um array JSON do meio de conteúdo HTML

        Args:
            conteudo: String que pode conter HTML + JSON

        Returns:
            Lista de pares [timestamp, valor]
        """
        # Tenta encontrar um array JSON usando regex
        # Procura por [... ] ou [[...], [...]] no conteúdo
        padrao = r"\[\[.*?\]\]"
        match = re.search(padrao, conteudo)

        if match:
            try:
                # Tenta parsear o JSON encontrado
                dados = json.loads(match.group(0))
                return dados
            except json.JSONDecodeError:
                pass

        # Fallback: tenta extrair todos os arrays de pares
        padrao_pares = r"\[\s*(\d+)\s*,\s*([\d.]+)\s*\]"
        matches = re.findall(padrao_pares, conteudo)

        if matches:
            dados = []
            for timestamp_str, valor_str in matches:
                dados.append([int(timestamp_str), float(valor_str)])
            return dados

        # Último recurso: tentar parsear o conteúdo inteiro como JSON
        try:
            # Remove tags HTML
            conteudo_limpo = re.sub(r"<[^>]+>", "", conteudo)
            # Tenta encontrar qualquer array JSON
            inicio = conteudo_limpo.find("[")
            fim = conteudo_limpo.rfind("]")
            if inicio != -1 and fim != -1:
                json_str = conteudo_limpo[inicio : fim + 1]
                return json.loads(json_str)
        except:
            pass

        return []

    def _subtrair_meses(self, data: datetime, meses: int) -> datetime:
        """Subtrai 'meses' de uma data, ajustando o dia se o mês de destino for mais curto
        (ex: 31/01 - 1 mês -> 28/02, não 31/02)."""

        mes_total = data.month - 1 - meses
        ano = data.year + mes_total // 12
        mes = mes_total % 12 + 1

        dia = min(data.day, monthrange(ano, mes)[1])

        return data.replace(year=ano, month=mes, day=dia)

    def calcular_data_corte(self, periodo: Optional[str]) -> Optional[datetime]:
        PERIODOS_VALIDOS = {
            "1mo": 1,
            "3mo": 3,
            "6mo": 6,
            "1y": 12,
            "ytd": None,
            "all": None,
        }
        """Retorna a data mínima a manter no histórico, ou None se não houver filtro (ex: 'all')."""
        if periodo is None:
            return None

        periodo = periodo.lower()

        if periodo not in PERIODOS_VALIDOS:
            opcoes = ", ".join(PERIODOS_VALIDOS)
            raise ValueError(f"Período inválido: '{periodo}'. Use um de: {opcoes}")

        hoje = datetime.now()

        if periodo == "all":
            return None
        if periodo == "ytd":
            return hoje.replace(
                month=1, day=1, hour=0, minute=0, second=0, microsecond=0
            )

        meses = PERIODOS_VALIDOS[periodo]

        return self._subtrair_meses(hoje, meses)

    def processar_historico_fundamentus(
        self,
        dados_brutos: List[List],
        periodo: Optional[str] = None,
        sortOrder: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Processa os dados históricos do Fundamentus e ordena por data (mais recente primeiro)

        Args:
            dados_brutos: Lista de pares [timestamp_ms, valor]

        Returns:
            Lista de dicionários com data e valor formatados, ordenados do mais recente para o mais antigo
        """
        historico_processado = []
        data_corte = self.calcular_data_corte(periodo)

        for item in dados_brutos:
            if not isinstance(item, list) or len(item) != 2:
                continue

            timestamp_ms = item[0]
            valor = item[1]

            data = datetime.fromtimestamp(timestamp_ms / 1000)
            if data_corte is not None and data < data_corte:
                continue
            historico_processado.append(
                {
                    "data": data.strftime("%Y-%m-%d"),
                    "data_completa": data.strftime("%d/%m/%Y"),
                    "date": data.isoformat().replace("+00:00", "Z"),
                    "timestamp": timestamp_ms,
                    "close": float(valor),
                    "adjustedClose": float(valor),
                    "dia_semana": data.strftime("%A"),
                    "mes": data.strftime("%B"),
                    "ano": data.year,
                    "mes_numero": data.month,
                    "dia": data.day,
                }
            )

        # ORDENA POR TIMESTAMP (mais recente primeiro)
        historico_processado.sort(key=lambda x: x["timestamp"], reverse=sortOrder)
        return historico_processado

    def _is_fii(self, dados: dict) -> bool:
        """
        Identifica se o ativo é FII baseado nos dados retornados.
        """
        # Opção 1: Verifica se tem o campo "FII" (vindo do Fundamentus)
        if dados.get("FII"):
            return True

        # Opção 2: Verifica se o ticker termina com 11 (padrão FII)
        ticker = dados.get("ticker", "") or dados.get("Papel", "")
        if ticker and ticker.upper().endswith("11"):
            return True

        # Opção 3: Verifica se tem campos específicos de FII
        if dados.get("Segmento") in [
            "Shopping",
            "Logística",
            "Lajes Corporativas",
            "Educacional",
        ]:
            return True

        return False

    def _parse_list_html(self, html: str):
        resultados = []
        soup = BeautifulSoup(html, "html.parser")
        tabelas = soup.find_all("table", id="test1")
        if len(tabelas) == 0:
            raise HTTPException(404, f"Fundo não encontrado no Fundamentus")
        for tabela in tabelas:
            linhas = tabela.find_all("tr")
            for linha in linhas:
                colunas = linha.find_all("td")
                if len(colunas) >= 3:
                    # Extrair dados básicos
                    ticker_cell = colunas[0]
                    nome_cell = colunas[1]
                    razao_social_cell = colunas[2]

                    # Extrair o ticker (link)
                    link = ticker_cell.find("a")

                    if link:
                        ticker = link.text.strip()

                        href = link.get("href")  # Ex: "detalhes.php?papel=VIVT4"

                        # Extrair nome e razão social
                        nome = nome_cell.text.strip() if nome_cell else ""
                        razao_social = (
                            razao_social_cell.text.strip() if razao_social_cell else ""
                        )

                        # Extrair outras colunas se existirem
                        # Você pode ter mais colunas dependendo da tabela
                        dados = {
                            "ticker": ticker,
                            "nome": nome,
                            "razao_social": razao_social,
                            "href": href,
                        }

                        resultados.append(dados)
        return resultados

    def _parse_proventos_html(self, html: str):
        dados = []
        soup = BeautifulSoup(html, "html.parser")
        tabelas = soup.find_all("table", id="resultado")
        if len(tabelas) == 0:
            raise HTTPException(
                404, f"Provento do ticker não encontrado no Fundamentus"
            )
        for tabela in tabelas:
            linhas = tabela.find_all("tr")
            for linha in linhas:
                colunas = linha.find_all("td")
                if len(colunas) >= 4:
                    competencia = colunas[0].text.strip()
                    pagamento = colunas[2].text.strip()
                    valor = colunas[3].text.strip()
                    resultado = {
                        "Competencia": competencia,
                        "DataPagamento": pagamento,
                        "ValorProvento": valor,
                    }
                    dados.append(resultado)
        return dados[0] if dados else None

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
