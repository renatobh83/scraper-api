import asyncio
import json
import logging
import os
import re
from calendar import monthrange
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx
from bs4 import BeautifulSoup
from fastapi import HTTPException

from app.scrapers.adapters.Fundamentus.fundamentus_type import build_response

logger = logging.getLogger("uvicorn.error")


class FundamentusHeadlinesScraper:
    """Scraper e adaptador de dados financeiros do portal Fundamentus.

    Responsabilidade:
    - Buscar cotações, detalhes cadastrais e proventos de Ações e FIIs usando HTTP assíncrono (httpx.AsyncClient).
    - Processar históricos numéricos de cotações passadas.
    - Executar requisições de forma não-bloqueante para o event loop do FastAPI.
    """

    source_name = "fundamentus"
    baseUrl = "https://www.fundamentus.com.br/detalhes.php?papel="
    historico = "https://www.fundamentus.com.br/amline/cot_hist.php?papel="
    proventos = "https://www.fundamentus.com.br/fii_proventos.php?papel="

    def _get_headers(self, ticker: Optional[str] = None) -> Dict[str, str]:
        """Retorna os cabeçalhos HTTP necessários simulando navegador real."""
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }
        if ticker:
            headers["Referer"] = (
                f"https://www.fundamentus.com.br/detalhes.php?papel={ticker}"
            )
        return headers

    async def search(self, ticker: str):
        """Busca detalhes de um ticker ou lista ativos correspondentes."""
        if not ticker:
            raise HTTPException(status_code=400, detail="O Ticker deve ser informado.")

        url_detalhes = f"{self.baseUrl}{ticker}"
        proxy_url = os.getenv("PROXY_URL")

        async with httpx.AsyncClient(
            verify=False,
            proxy=proxy_url,
            headers=self._get_headers(),
            timeout=10.0,
        ) as client:
            r = await client.get(url_detalhes)
            r.raise_for_status()

            soup = BeautifulSoup(r.text, "html.parser")
            has_detalhes = soup.select_one("table.w728")
            has_lista = soup.select_one("table#test1")

            if has_detalhes:
                dados_detalhes = self._parse_html(r.text)
                if self._is_fii(dados_detalhes):
                    dados_detalhes["proventos"] = await self._proventosHttpx(
                        client, ticker
                    )
                else:
                    dados_detalhes["proventos"] = None

                return build_response([dados_detalhes])

            if has_lista:
                dados_lista = self._parse_list_html(r.text)
                resultados_filtrados = [
                    item
                    for item in dados_lista
                    if (
                        ticker.lower() in item.get("ticker", "").lower()
                        or ticker.lower() in item.get("nome", "").lower()
                        or ticker.lower() in item.get("razao_social", "").lower()
                    )
                ]
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

                detalhes_completos = await self._buscar_detalhes_da_lista_httpx(
                    client, tickers_encontrados
                )

                for i, item in enumerate(detalhes_completos):
                    if self._is_fii(item):
                        detalhes_completos[i]["proventos"] = await self._proventosHttpx(
                            client, item.get("ticker", "")
                        )
                    else:
                        detalhes_completos[i]["proventos"] = None

                return build_response(detalhes_completos)

            raise HTTPException(
                status_code=404, detail="Ticker não encontrado no Fundamentus"
            )

    async def historicalFundamentusHttpx(self, ticker: str, interval: str, order: bool):
        """Busca o histórico diário de preços de um ativo via endpoint JSON do Fundamentus."""
        if not ticker:
            raise HTTPException(status_code=400, detail="O Ticker deve ser informado.")

        url_historico_api = f"{self.historico}{ticker}"
        proxy_url = os.getenv("PROXY_URL")

        try:
            async with httpx.AsyncClient(
                verify=False,
                proxy=proxy_url,
                headers=self._get_headers(ticker),
                timeout=10.0,
            ) as client:
                r = await client.get(url_historico_api)
                r.raise_for_status()

                html = r.text
                limpo = html.replace("\n", "").replace(" ", "")
                while ",," in limpo:
                    limpo = limpo.replace(",,", ",")
                limpo = limpo.replace("[,", "[").replace(",]", "]")
                dados = json.loads(limpo)

                historico = self.processar_historico_fundamentus(
                    dados, periodo=interval, sortOrder=order
                )

                return {
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
        except httpx.HTTPError as e:
            logger.error("Erro HTTP ao buscar histórico para %s: %s", ticker, e)
            raise HTTPException(
                status_code=502, detail=f"Erro ao buscar histórico: {e}"
            ) from e

    async def _proventosHttpx(
        self, client: httpx.AsyncClient, ticker: str
    ) -> Optional[dict]:
        """Busca a tabela de proventos distribuídos por um FII."""
        url_proventos = f"{self.proventos}{ticker}&tipo=2"
        try:
            response = await client.get(
                url_proventos, headers=self._get_headers(ticker)
            )
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")

            tabela = soup.find("table", id="resultado")
            if tabela is None:
                return None

            linhas = tabela.find_all("tr")[1:3]
            for linha in linhas:
                cols = linha.find_all("td")
                if len(cols) < 4:
                    continue

                valor_str = (
                    cols[3].get_text(strip=True).replace(".", "").replace(",", ".")
                )
                try:
                    valor = float(valor_str)
                except ValueError:
                    continue

                return {
                    "competencia": cols[0].get_text(strip=True),
                    "dataPagamento": cols[2].get_text(strip=True),
                    "valor": valor,
                }
            return None
        except Exception as e:
            logger.warning("Erro ao buscar proventos para %s: %s", ticker, e)
            return None

    async def _fetch_detalhes_httpx(
        self, client: httpx.AsyncClient, ticker: str
    ) -> Dict[str, Any]:
        """Obtém o HTML da página de detalhes e realiza a extração dos atributos."""
        url_detalhes = f"{self.baseUrl}{ticker}"
        response = await client.get(url_detalhes)
        response.raise_for_status()
        return self._parse_html(response.text)

    async def _buscar_detalhes_da_lista_httpx(
        self, client: httpx.AsyncClient, tickers: List[str], max_concorrentes: int = 3
    ) -> List[Dict[str, Any]]:
        """Realiza requisições em lote paralelas limitadas por um semáforo interno."""
        semaforo = asyncio.Semaphore(max_concorrentes)
        resultados: List[Dict[str, Any]] = []

        async def _buscar_um(tk: str):
            async with semaforo:
                try:
                    dados = await self._fetch_detalhes_httpx(client, tk)
                    resultados.append(dados)
                except Exception as e:
                    logger.warning("Falha ao buscar detalhes de %s: %s", tk, e)

        await asyncio.gather(*(_buscar_um(tk) for tk in tickers))
        return resultados

    def processar_historico_fundamentus(
        self,
        dados_brutos: List[List],
        periodo: Optional[str] = None,
        sortOrder: bool = False,
    ) -> List[Dict[str, Any]]:
        """Formata a lista bruta de timestamps e preços em um dicionário padronizado."""
        historico_processado = []
        data_corte = self.calcular_data_corte(periodo)

        for item in dados_brutos:
            if not isinstance(item, list) or len(item) != 2:
                continue
            timestamp_ms, valor = item[0], item[1]

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
                    "ano": data.year,
                    "mes_numero": data.month,
                    "dia": data.day,
                }
            )

        historico_processado.sort(key=lambda x: x["timestamp"], reverse=sortOrder)
        return historico_processado

    def calcular_data_corte(self, periodo: Optional[str]) -> Optional[datetime]:
        """Calcula o limite temporal com base no período solicitado (ex: 1mo, 1y)."""
        PERIODOS_VALIDOS = {
            "1mo": 1,
            "3mo": 3,
            "6mo": 6,
            "1y": 12,
            "ytd": None,
            "all": None,
        }
        if not periodo:
            return None

        periodo_clean = periodo.lower()
        if periodo_clean not in PERIODOS_VALIDOS:
            return None

        hoje = datetime.now()
        if periodo_clean == "all":
            return None
        if periodo_clean == "ytd":
            return hoje.replace(
                month=1, day=1, hour=0, minute=0, second=0, microsecond=0
            )

        meses = PERIODOS_VALIDOS[periodo_clean]
        return self._subtrair_meses(hoje, meses) if meses else None

    def _subtrair_meses(self, data: datetime, meses: int) -> datetime:
        """Subtrai meses ajustando o último dia caso a data seja inválida (ex: 31 de Fev)."""
        mes_total = data.month - 1 - meses
        ano = data.year + mes_total // 12
        mes = mes_total % 12 + 1
        dia = min(data.day, monthrange(ano, mes)[1])
        return data.replace(year=ano, month=mes, day=dia)

    def _is_fii(self, dados: dict) -> bool:
        """Verifica se os dados extraídos indicam um Fundo de Investimento Imobiliário."""
        if dados.get("FII"):
            return True
        ticker = dados.get("ticker", "") or dados.get("Papel", "")
        if ticker and ticker.upper().endswith("11"):
            return True
        return False

    def _parse_list_html(self, html: str) -> List[Dict[str, str]]:
        """Extrai linhas da tabela de resultados de buscas por texto."""
        resultados = []
        soup = BeautifulSoup(html, "html.parser")
        tabelas = soup.find_all("table", id="test1")
        for tabela in tabelas:
            for linha in tabela.find_all("tr"):
                colunas = linha.find_all("td")
                if len(colunas) >= 3:
                    link = colunas[0].find("a")
                    if link:
                        resultados.append(
                            {
                                "ticker": link.text.strip(),
                                "nome": colunas[1].text.strip(),
                                "razao_social": colunas[2].text.strip(),
                                "href": link.get("href", ""),
                            }
                        )
        return resultados

    def _parse_html(self, html: str) -> Dict[str, str]:
        """Parseia tabelas de chave/valor da página de detalhes do papel."""
        dados = {}
        soup = BeautifulSoup(html, "html.parser")
        tabelas = soup.find_all("table", class_="w728")
        if not tabelas:
            raise HTTPException(404, "Fundo/Papel não encontrado no Fundamentus")

        for tabela in tabelas:
            for linha in tabela.find_all("tr"):
                colunas = linha.find_all("td")
                for i in range(0, len(colunas), 2):
                    chave = colunas[i].get_text(strip=True).replace("?", "")
                    if i + 1 < len(colunas):
                        dados[chave] = colunas[i + 1].get_text(strip=True)
        return dados
