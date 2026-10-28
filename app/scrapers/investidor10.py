from urllib.parse import urljoin
import requests
from app.scrapers.adapters.Investidor.investidor_Type import (
    _limpar_chave,
    build_response,
    build_response_fundamentals,
    formatar_valor,
    remover_chaves_duplicadas,
)
from app.scrapers.base import QuoteScraper
from fastapi import HTTPException

import httpx
from bs4 import BeautifulSoup, Tag


class Investidor10Scraper(QuoteScraper):
    source_name = "investidor10"
    baseUrl = "https://investidor10.com.br"
    urlBusca = f"{baseUrl}/busca/?s="

    async def scrape(self, ticker: str):
        pass

    async def search(self, ticker: str):
        if not ticker:
            raise HTTPException(
                status_code=503,
                detail=("O Ticker deve ser informado."),
            )
        busca = f"{self.urlBusca}{ticker}"

        try:
            with httpx.Client(
                verify=False,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
                    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
                },
                timeout=10.0,
            ) as client:

                r = client.get(busca)
                r.raise_for_status()

                soup = BeautifulSoup(r.text, "html.parser")

                has_lista = soup.select_one("div.search-category-results")

                if has_lista:
                    dados_lista = self._parse_list_html(r.text)
                    resultados_filtrados = [
                        item
                        for item in dados_lista
                        if (
                            ticker.lower() in (item.get("ticker") or "").lower()
                            or ticker.lower() in (item.get("nome") or "").lower()
                            or ticker.lower()
                            in (item.get("razao_social") or "").lower()
                        )
                    ]
                    return build_response(resultados_filtrados)
        except:
            raise

    async def fundamentals(self, url: str, kind: str, ticker: str):
        if not url:
            raise HTTPException(
                status_code=503,
                detail=("A url deve ser informado."),
            )
        try:
            with httpx.Client(
                verify=False,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
                    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
                },
                timeout=10.0,
            ) as client:
                r = client.get(url)
                r.raise_for_status()
                cardHeader = self._parse_cardHeader_fundamentals(r.text, ticker)

                if kind == "stock":
                    fundamentals = self._parse_stock_html(r.text)
                if kind == "fii":
                    fundamentals = self._parse_fii_html(r.text)
                dados = {
                    **cardHeader,
                    **fundamentals,
                    "sub_type": kind,
                    "ticker": ticker,
                }

                return build_response_fundamentals([dados])  # type: ignore
        except:
            raise

    def _parse_cardHeader_fundamentals(self, html: str, ticker: str):
        dados = {}

        soup = BeautifulSoup(html, "html.parser")
        secao = soup.find("section", id="cards-ticker")

        if not isinstance(secao, Tag):
            raise Exception(404, "Seção de cards não encontrada")

        cards = secao.select("div._card")

        for card in cards:
            header = card.find("div", class_="_card-header")
            body = card.find("div", class_="_card-body")
            if isinstance(header, Tag) and isinstance(body, Tag):
                # Pega o título (ex: "Cotação", "VARIAÇÃO (12M)")
                chave = header.get_text(strip=True)
                chave_limpa = _limpar_chave(chave, ticker)
                # --- TRATAMENTO DO VALOR ---
                # Precisamos ignorar os textos de tooltip e as tags SVG
                valor = ""
                # 1. Remove o badge de variação diária (do card de Cotação)
                for badge in body.find_all("span", class_="daily-variation-badge"):
                    badge.decompose()
                # 2. Remove todos os tooltips
                for tooltip in body.find_all("span", class_="ds-tooltip-trigger"):
                    tooltip.decompose()
                # 3. Remove todos os SVGs (setinhas)
                for svg in body.find_all("svg"):
                    svg.decompose()
                if "Cotação" in chave_limpa:
                    # A cotação tem o valor dentro de uma span class="value"
                    val_el = body.find("span", class_="value")

                    if isinstance(val_el, Tag):
                        valor = val_el.get_text(strip=True)
                else:
                    # Pega o texto limpo (ex: "85,41%")
                    valor = body.get_text(strip=True)

                if chave_limpa and valor:
                    dados[chave_limpa] = valor
        return dados

    def _parse_stock_html(self, html: str):
        dados = {}

        soup = BeautifulSoup(html, "html.parser")

        cardIndicadores = soup.select(".indicator-card")

        # print(f"Registros indicadores encontrados: {len(cardIndicadores)}\n")
        for card in cardIndicadores:
            header = card.find("div", class_="indicator-card-title")
            body = card.find("div", class_="indicator-card-value")
            if isinstance(header, Tag) and isinstance(body, Tag):
                chave = header.get_text(strip=True)
                valor = ""
                valor = body.get_text(strip=True)
                if chave and valor:
                    dados[chave] = valor
        return dados

    def _parse_fii_html(self, html: str):
        dados = {}
        import re

        html_limpo = re.sub(r"<script.*?</script>", "", html, flags=re.DOTALL)
        html_limpo = re.sub(r"<style.*?</style>", "", html_limpo, flags=re.DOTALL)
        # 1. Tenta usar o lxml. Se não estiver instalado, cai no html.parser
        try:
            soup = BeautifulSoup(html_limpo, "lxml")
        except Exception:
            soup = BeautifulSoup(html_limpo, "html.parser")

        desc = soup.select(".desc")

        for card in desc:
            header = card.find(
                "span", class_="d-flex justify-content-between align-items-center name"
            )
            body = card.find("div", class_="value")
            if isinstance(header, Tag) and isinstance(body, Tag):
                chave = header.get_text(strip=True)
                valor = ""
                valor = body.get_text(strip=True)
                if chave and valor:
                    dados[chave] = valor
        # --- PARTE 2: TABELA HISTÓRICO ---
        id_ativo = None
        for tag in soup.find_all(attrs={"data-id": True}):
            if isinstance(tag, Tag):
                # Pega o valor do data-id
                valor_id = tag.get("data-id")
                if valor_id and str(valor_id).isdigit():
                    id_ativo = str(valor_id)
                    break
        # Se não achou via BeautifulSoup, tenta via Regex
        if not id_ativo:
            match = re.search(r'data-id="(\d+)"', html)
            if match:
                id_ativo = match.group(1)
        if not id_ativo:
            print("❌ Não foi possível encontrar o ID do ativo no HTML.")
            return dados

        # print(f"✅ ID do ativo encontrado: {id_ativo}")
        url_api = (
            f"https://investidor10.com.br/api/fii/historico-indicadores/{id_ativo}/5"
        )
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",  # Avisa que é uma chamada AJAX
        }

        try:
            response = requests.get(url_api, headers=headers)
            response.raise_for_status()
            json_dados = response.json()

        except:
            pass
        # 3. Processar o JSON da API
        # A API geralmente retorna algo como: {"Valor de Mercado": ["R$ 6,39 B", "R$ 6,33 B"...]}
        # ou {"indicators": [{"name": "Valor de Mercado", "values": [...]}]}
        # Se for um dicionário onde a chave é o nome e o valor é um array:
        for chave, valores in json_dados.items():

            # Pega o item do ano atual (geralmente é uma lista onde o primeiro item é o atual)
            item_atual = (
                valores[0]
                if isinstance(valores, list) and len(valores) > 0
                else valores
            )
            if isinstance(item_atual, dict) and "value" in item_atual:

                # Aqui está a correção: pegamos APENAS o valor numérico
                dados[chave] = formatar_valor(item_atual["value"], item_atual["type"])

            elif item_atual is not None:
                # Para dados simples como Razão Social, CNPJ, etc.
                dados[chave] = item_atual

        remover_chaves_duplicadas(dados)
        return dados

    def _parse_list_html(self, html: str):
        resultados = []

        soup = BeautifulSoup(html, "html.parser")

        cards = soup.select("div.search-category-results > a.search-result-card")
        assetType = soup.select_one(".search-category-title")
        assetType = assetType.get_text(strip=True) if assetType else None

        for i, card in enumerate(cards, start=1):

            href_attr = card.get("href")
            if isinstance(href_attr, list):
                href = href_attr[0] if href_attr else ""
            elif isinstance(href_attr, str):
                href = href_attr
            else:
                href = ""
            href = href.strip()
            full_url = urljoin(self.baseUrl, href)
            logo_el = card.select_one(".search-result-thumb")
            logo_src = logo_el.get("src") if logo_el else None

            ticker = card.select_one(".search-result-ticker")
            ticker = ticker.get_text(strip=True) if ticker else None

            name = card.select_one(".search-result-name")
            name = name.get_text(strip=True) if name else None

            price = card.select_one(".search-result-price")
            price = price.get_text(strip=True) if price else None

            var_el = card.select_one(".search-result-variation")
            if var_el:
                # remove o SVG para pegar só o texto "90,38%"
                for svg in var_el.find_all("svg"):
                    svg.decompose()
                variation = var_el.get_text(strip=True)
                direction = "up" if "up" in (var_el.get("class") or []) else "down"
            else:
                variation = direction = None

            period = card.select_one(".search-result-period")
            period = period.get_text(strip=True) if period else None

            dados = {
                "ticker": ticker,
                "nome": name,
                "assetType": assetType,
                "razao_social": name,
                "href": full_url,
                "currency": price,
                "logoUrl": f"{self.baseUrl}{logo_src}",
                "changePercent": variation,
                "period": period,
            }
            resultados.append(dados)
        return resultados
