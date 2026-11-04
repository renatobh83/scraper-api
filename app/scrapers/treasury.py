import asyncio
import csv
import io
import json
import time
import urllib.request
from datetime import datetime, date, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo
import urllib.error
from app.scrapers.base import TreasuryScraper as BaseTreasuryScraper

TZ_BR = ZoneInfo("America/Sao_Paulo")
CHECK_INTERVAL = 3600
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; nexus-scraper/1.0)"}


def parse_num(s: str | None) -> float | None:
    s = (s or "").strip()
    if not s:
        return None
    return float(s.replace(",", "."))


@lru_cache(maxsize=4096)
def parse_date_br(s: str) -> date:
    return datetime.strptime(s.strip(), "%d/%m/%Y").date()


class TreasuryScraper(BaseTreasuryScraper):
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

    def _get(self, url: str) -> bytes:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read()

    def get_csv_url(self) -> str:
        data = json.loads(self._get(self.api_url).decode("utf-8"))
        for res in data["result"]["resources"]:
            if res.get("format", "").upper() == "CSV":
                return res["url"]
        raise RuntimeError("Recurso CSV não encontrado no dataset.")

    def fetch_csv_text(self, url: str) -> str:
        return self._get(url).decode("utf-8-sig")

    @lru_cache(maxsize=2048)
    def to_symbol(self, bond_type: str, maturity: date) -> str:
        base = "-".join(bond_type.lower().replace("+", "").split())
        return f"{base}-{maturity.strftime('%d%m%Y')}"

    @lru_cache(maxsize=64)
    def to_indexer(self, bond_type: str) -> str:
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
        ref = reference or datetime.now(TZ_BR).date()
        reader = csv.DictReader(io.StringIO(csv_text), delimiter=";")
        by_symbol: dict[str, tuple[date, dict]] = {}

        for row in reader:
            bond_type = (row.get("Tipo Titulo") or "").strip()
            if not bond_type:
                continue

            maturity = parse_date_br(row["Data Vencimento"])
            if maturity < ref:  # descarta vencidos antes de qualquer outro parse
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

    def _fetch_if_changed(self) -> dict | None:
        cls = type(self)
        if not cls._csv_url:
            cls._csv_url = self.get_csv_url()

        headers = dict(HEADERS)
        if cls._cache:
            if cls._etag:
                headers["If-None-Match"] = cls._etag
            if cls._last_modified:
                headers["If-Modified-Since"] = cls._last_modified

        req = urllib.request.Request(cls._csv_url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                cls._etag = r.headers.get("ETag")
                cls._last_modified = r.headers.get("Last-Modified")
                return self.build_payload(r.read().decode("utf-8-sig"))
        except urllib.error.HTTPError as e:
            if e.code == 304:  # não mudou
                return None
            raise

    async def _get_payload(self) -> dict:
        cls = type(self)
        if cls._cache and time.monotonic() - cls._cache_ts < CHECK_INTERVAL:
            return cls._cache
        async with cls._lock:
            if cls._cache and time.monotonic() - cls._cache_ts < CHECK_INTERVAL:
                return cls._cache
            try:
                new = await asyncio.to_thread(self._fetch_if_changed)
            except Exception:
                if cls._cache:  # falhou a checagem: serve o cache antigo
                    return cls._cache
                raise
            if new is not None:
                cls._cache = new
            cls._cache_ts = time.monotonic()
            return cls._cache

    def filter_results(self, results: list[dict], q: str | None) -> list[dict]:
        if not q or not q.strip():
            return results
        tokens = q.lower().split()
        return [r for r in results if all(t in r["_haystack"] for t in tokens)]

    async def search(self, q: str | None):
        payload = await self._get_payload()
        results = self.filter_results(payload["results"], q)
        # remove o campo interno antes de devolver
        return {
            "status": "ok",
            "results": [
                {k: v for k, v in r.items() if k != "_haystack"} for r in results
            ],
        }

    def to_coupon_type(self, bond_type: str) -> str:
        return "semiannual" if "juros semestrais" in bond_type.lower() else "zero"

    async def indicators(self, symbols: list[str]) -> dict:
        payload = await self._get_payload()
        index = payload["index"]

        # normaliza e remove duplicados mantendo a ordem
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
