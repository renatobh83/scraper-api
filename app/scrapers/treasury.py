import asyncio
import csv
import io
import json
import logging
import time
from datetime import date, datetime, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

import httpx

from app.scrapers.base import TreasuryScraper as BaseTreasuryScraper

logger = logging.getLogger("uvicorn.error")

TZ_BR = ZoneInfo("America/Sao_Paulo")
CHECK_INTERVAL = 3600
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; nexus-scraper/1.0)"}


def parse_num(s: str | None) -> float | None:
    """Converte números formatados em string (com vírgula) para float."""
    s = (s or "").strip()
    if not s:
        return None
    return float(s.replace(",", "."))


@lru_cache(maxsize=4096)
def parse_date_br(s: str) -> date:
    """Parseia datas no formato brasileiro DD/MM/YYYY com cache em memória."""
    return datetime.strptime(s.strip(), "%d/%m/%Y").date()


class TreasuryScraper(BaseTreasuryScraper):
    """Scraper dos títulos do Tesouro Direto via dados abertos da API do governo.

    Responsabilidade:
    - Consultar o catálogo de dados abertos para obter a URL do arquivo CSV atualizado.
    - Baixar e processar o arquivo de taxas de títulos de forma 100% assíncrona (httpx.AsyncClient).
    - Manter em memória um cache de curto prazo evitando requisições excessivas à infraestrutura pública.
    """

    source_name = "treasury"
    dataset_id = "taxas-dos-titulos-ofertados-pelo-tesouro-direto"
    api_url = (
        "https://www.tesourotransparente.gov.br/ckan/api/3/action/"
        f"package_show?id={dataset_id}"
    )

    _cache: dict | None = None
    _cache_ts: float = 0.0
    _lock = asyncio.Lock()
    _csv_url: str | None = None
    _etag: str | None = None
    _last_modified: str | None = None

    async def get_csv_url(self) -> str:
        """Consulta a API do portal de dados abertos e recupera o link direto do CSV."""
        async with httpx.AsyncClient(headers=HEADERS, timeout=30.0) as client:
            logger.info("Inicio de scrapy in", self.api_url)
            resp = await client.get(self.api_url)
            resp.raise_for_status()
            data = resp.json()
            for res in data["result"]["resources"]:
                if res.get("format", "").upper() == "CSV":
                    return res["url"]
        raise RuntimeError("Recurso CSV não encontrado no dataset do Tesouro Direto.")

    @lru_cache(maxsize=2048)
    def to_symbol(self, bond_type: str, maturity: date) -> str:
        """Gera o identificador normalizado do título (ex: tesouro-ipca-15082026)."""
        base = "-".join(bond_type.lower().replace("+", "").split())
        return f"{base}-{maturity.strftime('%d%m%Y')}"

    @lru_cache(maxsize=64)
    def to_indexer(self, bond_type: str) -> str:
        """Mapeia o índice do título (selic, ipca, prefixado, igpm)."""
        bt = bond_type.lower()
        if "selic" in bt:
            return "selic"
        if "prefixado" in bt:
            return "prefixado"
        if "ipca" in bt:
            return "ipca"
        if "igpm" in bt or "igp-m" in bt:
            return "igpm"
        return "desconhecido"

    def build_payload(self, csv_text: str, reference: date | None = None) -> dict:
        """Processa as linhas do CSV e gera os dados estruturados de taxas e títulos."""
        ref = reference or datetime.now(TZ_BR).date()
        reader = csv.DictReader(io.StringIO(csv_text), delimiter=";")
        by_symbol: dict[str, tuple[date, dict, dict]] = {}

        for row in reader:
            bond_type = (row.get("Tipo Titulo") or "").strip()
            if not bond_type:
                continue

            maturity = parse_date_br(row["Data Vencimento"])
            if maturity < ref:
                continue

            base_date = parse_date_br(row["Data Base"])
            symbol = self.to_symbol(bond_type, maturity)

            prev = by_symbol.get(symbol)
            if prev and prev[0] >= base_date:
                continue

            item = {
                "symbol": symbol,
                "name": f"{bond_type} {maturity.year}",
                "bondType": bond_type,
                "indexer": self.to_indexer(bond_type),
                "maturityDate": maturity.isoformat(),
                "baseDate": base_date.isoformat(),
            }
            quote = {
                "couponType": self.to_coupon_type(bond_type),
                "buyRate": parse_num(row.get("Taxa Compra Manha")),
                "sellRate": parse_num(row.get("Taxa Venda Manha")),
                "buyPrice": parse_num(row.get("PU Compra Manha")),
                "sellPrice": parse_num(row.get("PU Venda Manha")),
                "basePrice": parse_num(row.get("PU Base Manha")),
                "source": "Tesouro Nacional",
            }
            item["_haystack"] = " ".join(
                (symbol, item["name"], bond_type, item["indexer"], item["maturityDate"])
            ).lower()
            by_symbol[symbol] = (base_date, item, quote)

        results = sorted(
            (v[1] for v in by_symbol.values()),
            key=lambda x: (x["bondType"], x["maturityDate"]),
        )
        index = {v[1]["symbol"]: {**v[1], **v[2]} for v in by_symbol.values()}
        return {"status": "ok", "results": results, "index": index}

    async def _fetch_if_changed(self) -> dict | None:
        """Valida ETag e Last-Modified para evitar re-download do CSV quando inalterado."""
        cls = type(self)
        if not cls._csv_url:
            cls._csv_url = await self.get_csv_url()

        headers = dict(HEADERS)
        if cls._cache:
            if cls._etag:
                headers["If-None-Match"] = cls._etag
            if cls._last_modified:
                headers["If-Modified-Since"] = cls._last_modified

        async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
            resp = await client.get(cls._csv_url)
            if resp.status_code == 304:
                return None

            resp.raise_for_status()
            cls._etag = resp.headers.get("ETag")
            cls._last_modified = resp.headers.get("Last-Modified")
            return self.build_payload(resp.text)

    async def _get_payload(self) -> dict:
        """Garante acesso thread-safe ao payload em cache ou renova o conteúdo."""
        cls = type(self)
        if cls._cache and time.monotonic() - cls._cache_ts < CHECK_INTERVAL:
            return cls._cache

        async with cls._lock:
            if cls._cache and time.monotonic() - cls._cache_ts < CHECK_INTERVAL:
                return cls._cache

            try:
                new = await self._fetch_if_changed()
                if new is not None:
                    cls._cache = new
            except Exception as e:
                logger.warning("Falha ao atualizar Tesouro Direto: %s", e)
                if cls._cache:
                    return cls._cache
                raise

            cls._cache_ts = time.monotonic()
            return cls._cache

    def filter_results(self, results: list[dict], q: str | None) -> list[dict]:
        """Filtra itens pela string de busca."""
        if not q or not q.strip():
            return results
        tokens = q.lower().split()
        return [r for r in results if all(t in r["_haystack"] for t in tokens)]

    async def search(self, q: str | None):
        """Método público de busca de títulos por termo ou código."""
        payload = await self._get_payload()
        results = self.filter_results(payload["results"], q)
        return {
            "status": "ok",
            "results": [
                {k: v for k, v in r.items() if k != "_haystack"} for r in results
            ],
        }

    def to_coupon_type(self, bond_type: str) -> str:
        return "semiannual" if "juros semestrais" in bond_type.lower() else "zero"

    async def indicators(self, symbols: list[str]) -> dict:
        """Retorna taxas e preços de uma lista específica de títulos."""
        payload = await self._get_payload()
        index = payload["index"]

        wanted = list(dict.fromkeys(s.strip().lower() for s in symbols if s.strip()))
        results, not_found = [], []

        for sym in wanted:
            item = index.get(sym)
            if item:
                results.append({k: v for k, v in item.items() if k != "_haystack"})
            else:
                not_found.append(sym)

        return {
            "status": "ok",
            "provider": "tesouro-direto-oficial",
            "requestedAt": datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "results": results,
            "notFound": not_found,
        }
