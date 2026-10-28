"""
Adapter: converte o payload bruto retornado pela sua API/scraper (estilo
Fundamentus, chaves em português, números no formato BR) para o schema
normalizado da plataforma (ticker, name, assetType, quote, fundamentals...).

Regras aplicadas (conforme especificação):
  - Números sempre puros no JSON: sem "R$" e sem "%".
  - quote.changePercent: percentual puro (-2,07% -> -2.07).
  - fundamentals com kind="percent": fração decimal (9% -> 0.09).
  - fundamentals com kind="number": valor numérico puro.

Uso:
    from adapter import build_asset, build_response
    asset = build_asset(RAW_SOURCE_DICT)
    payload = build_response([RAW_SOURCE_DICT, outro_raw_dict, ...])
"""

from __future__ import annotations
import unicodedata
import re

from datetime import datetime, timezone
from typing import Any, Optional

ASSET_TYPE_MAP = {
    "Ações": "stock",
    "FIIs": "fii",
    "ETFs": "etf",
    "BDRs": "bdr",
    "Tesouro Direto": "treasury",
    "Renda Fixa": "fixed_income",
}


def infer_asset_type(sub_type: Optional[str]) -> str:
    return "fund" if sub_type == "fii" else "stock"


def build_asset(source: dict[str, Any]):
    """Converte um dict bruto (retorno da sua API/scraper) para o objeto 'asset' normalizado."""

    papel = extract_ticker_and_sub_type(source)

    return {
        "symbol": source.get("ticker"),
        "name": source.get("nome"),
        "assetType": infer_asset_type(papel),
        "provento": source.get("proventos"),
        "href": source.get("href"),
        "subType": papel,
        "currency": "BRL",
        "active": True,
        "exchange": "B3",
        "sector": source.get("Segmento") if papel == "fii" else source.get("Setor"),
        "logoUrl": source.get("logoUrl"),
        "quote": {
            "lastPrice": parse_br_float(source.get("currency")),
            "changePercent": parse_br_percent(source.get("changePercent")),
            "period": source.get("period"),
        },
    }


def build_response(sources_list: list[dict[str, Any]]) -> dict[str, Any]:
    """Monta o payload final: {requestedAt, assets: [...]}."""
    return {
        "requestedAt": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "results": [build_asset(src) for src in sources_list],
    }


def formatar_valor(valor, tipo):
    if tipo == "money_abbr":
        if valor >= 1_000_000_000:
            return f"R$ {valor / 1_000_000_000:.2f} B".replace(".", ",")
        elif valor >= 1_000_000:
            return f"R$ {valor / 1_000_000:.2f} M".replace(".", ",")
        return f"R$ {valor:.2f}".replace(".", ",")
    elif tipo == "percent":
        return f"{valor:.2f}%".replace(".", ",")
    elif tipo == "decimal":
        return f"{valor:.2f}".replace(".", ",")
    elif tipo == "money":
        return f"R$ {valor:.2f}".replace(".", ",")
    elif tipo == "number_abbr":
        if valor >= 1_000_000:
            return f"{valor / 1_000_000:.2f} M".replace(".", ",")
        elif valor >= 1_000:
            return f"{valor / 1_000:.2f} mil".replace(".", ",")
        return str(valor)
    return str(valor)


def normalizar_chave(chave):
    """
    Normaliza uma chave para facilitar a comparação.

    Remove acentos, converte letras para minúsculas,
    substitui hífens e pontuações por espaços e elimina
    espaços duplicados.
    """
    chave = str(chave)

    # Remove acentos, por exemplo: "Â" -> "A"
    chave = unicodedata.normalize("NFKD", chave)
    chave = "".join(
        caractere for caractere in chave if not unicodedata.combining(caractere)
    )

    # Converte para letras minúsculas
    chave = chave.casefold()

    # Substitui pontuações por espaços
    chave = re.sub(r"[^a-z0-9]+", " ", chave)

    # Remove espaços extras
    return " ".join(chave.split())


def remover_chaves_duplicadas(dados, manter="primeiro"):
    """
    Remove chaves duplicadas após a normalização.

    O parâmetro `manter` aceita:
    - "primeiro": mantém o primeiro valor encontrado;
    - "ultimo": mantém o último valor encontrado.

    A chave original do dicionário é preservada no resultado.
    """
    resultado = {}
    chaves_normalizadas = {}

    for chave, valor in dados.items():
        chave_normalizada = normalizar_chave(chave)

        if chave_normalizada not in chaves_normalizadas:
            chaves_normalizadas[chave_normalizada] = chave
            resultado[chave] = valor

        elif manter == "ultimo":
            chave_original = chaves_normalizadas[chave_normalizada]
            resultado[chave_original] = valor

    return resultado


def extract_ticker_and_sub_type(
    source: dict[str, Any],
) -> Optional[str]:
    """
    A origem manda a ação em 'Papel' (ex: 'PETR4') e o FII em 'FII' (ex: 'VGHF11').
    Retorna (ticker, subType) — subType='fii' só quando veio pela chave 'FII'.
    """
    raw = source.get("assetType")
    if raw:
        return ASSET_TYPE_MAP.get(raw, raw)

    return None


def parse_br_float(value: Optional[str]) -> Optional[float]:
    """'561.304.000.000' -> 561304000000.0 | '43,55' -> 43.55 | '-21,26' -> -21.26"""
    if value is None:
        return None
    s = str(value).strip()
    if s == "" or s.lower() in {"n/a", "-", "null"}:
        return None
    s = s.replace("R$", "").replace("%", "").strip()
    # remove separador de milhar (ponto) e troca decimal (vírgula) por ponto
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def parse_br_percent(value: Optional[str]) -> Optional[float]:
    """'9,15%' -> 9.15 (percentual puro, usado em quote.changePercent)."""
    return parse_br_float(value)
