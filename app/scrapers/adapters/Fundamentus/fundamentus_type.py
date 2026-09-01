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

from datetime import datetime, timezone
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Helpers de parsing de números no formato brasileiro
# ---------------------------------------------------------------------------


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


def parse_br_percent_fraction(value: Optional[str]) -> Optional[float]:
    """'9,15%' -> 0.0915 (fração decimal, usada nos campos kind:'percent' de fundamentos)."""
    v = parse_br_float(value)
    return None if v is None else round(v / 100, 6)


def parse_br_int(value: Optional[str]) -> Optional[int]:
    v = parse_br_float(value)
    return None if v is None else int(v)


def parse_br_date(value: Optional[str]) -> Optional[str]:
    """'28/08/2026' -> '2026-08-28' (data ISO, sem horário pois a origem não fornece)."""
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%d/%m/%Y").date().isoformat()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Mapeamento dos indicadores fundamentalistas: chave de origem -> (label, kind)
# kind: "percent" (vira fração decimal) ou "number" (valor puro).
# Ajuste os labels/kind se o schema real de "fundamentals" da sua API for diferente.
# ---------------------------------------------------------------------------

FUNDAMENTAL_FIELDS: dict[str, tuple[str, str]] = {
    "P/L": ("P/L", "number"),
    "LPA": ("LPA", "number"),
    "P/VP": ("P/VP", "number"),
    "VPA": ("VPA", "number"),
    "P/EBIT": ("P/EBIT", "number"),
    "Marg. Bruta": ("Margem Bruta", "percent"),
    "PSR": ("PSR", "number"),
    "Marg. EBIT": ("Margem EBIT", "percent"),
    "P/Ativos": ("P/Ativos", "number"),
    "Marg. Líquida": ("Margem Líquida", "percent"),
    "P/Cap. Giro": ("P/Capital de Giro", "number"),
    "EBIT / Ativo": ("EBIT / Ativo", "percent"),
    "P/Ativ Circ Liq": ("P/Ativo Circulante Líquido", "number"),
    "ROIC": ("ROIC", "percent"),
    "Div. Yield": ("Dividend Yield", "percent"),
    "ROE": ("ROE", "percent"),
    "EV / EBITDA": ("EV/EBITDA", "number"),
    "Liquidez Corr": ("Liquidez Corrente", "number"),
    "EV / EBIT": ("EV/EBIT", "number"),
    "Dív Líq / Patrim": ("Dívida Líquida / Patrimônio", "number"),
    "Cres. Rec (5a)": ("Crescimento da Receita (5a)", "percent"),
    "Giro Ativos": ("Giro de Ativos", "number"),
}

# Variações históricas (Dia, Mês, 30 dias, 12 meses, anos-calendário) — todas percent
HISTORICAL_VARIATION_KEYS = {
    "Dia",
    "Mês",
    "30 dias",
    "12 meses",
    "2021",
    "2022",
    "2023",
    "2024",
    "2025",
    "2026",
}

# Campos de balanço em valores absolutos (R$) — number puro
BALANCE_SHEET_FIELDS = {
    "Ativo": "Ativo Total",
    "Dív. Bruta": "Dívida Bruta",
    "Disponibilidades": "Disponibilidades",
    "Dív. Líquida": "Dívida Líquida",
    "Ativo Circulante": "Ativo Circulante",
    "Patrim. Líq": "Patrimônio Líquido",
    "Receita Líquida": "Receita Líquida (12m)",
    "EBIT": "EBIT (12m)",
    "Lucro Líquido": "Lucro Líquido (12m)",
}


# Indicadores fundamentalistas de FII: chave de origem -> (label, kind)
FII_FUNDAMENTAL_FIELDS: dict[str, tuple[str, str]] = {
    "FFO Yield": ("FFO Yield", "percent"),
    "FFO/Cota": ("FFO por Cota", "number"),
    "Div. Yield": ("Dividend Yield", "percent"),
    "Dividendo/cota": ("Dividendo por Cota", "number"),
    "P/VP": ("P/VP", "number"),
    "VP/Cota": ("VP por Cota", "number"),
    "Cap Rate": ("Cap Rate", "percent"),
    "Vacância Média": ("Vacância Média", "percent"),
    "Imóveis/PL do FII": ("Imóveis / PL do Fundo", "percent"),
    "Aluguel/m2": ("Aluguel por m²", "number"),
    "Preço do m2": ("Preço por m²", "number"),
}

# Campos de balanço/operacionais de FII em valor absoluto — number puro
FII_BALANCE_FIELDS: dict[str, str] = {
    "Receita": "Receita",
    "Venda de ativos": "Venda de Ativos",
    "FFO": "FFO",
    "Rend. Distribuído": "Rendimento Distribuído",
    "Ativos": "Ativos Totais",
    "Patrim Líquido": "Patrimônio Líquido",
    "Qtd imóveis": "Quantidade de Imóveis",
    "Área (m2)": "Área (m²)",
    "Qtd Unidades": "Quantidade de Unidades",
}


def build_fundamentals(
    source: dict[str, Any], sub_type: Optional[str]
) -> list[dict[str, Any]]:
    """Monta a lista de indicadores no formato [{key, label, kind, value}, ...].

    Usa o mapeamento de FII quando sub_type == 'fii', senão o de ação.
    """
    field_map = FII_FUNDAMENTAL_FIELDS if sub_type == "fii" else FUNDAMENTAL_FIELDS
    balance_map = FII_BALANCE_FIELDS if sub_type == "fii" else BALANCE_SHEET_FIELDS
    indicators: list[dict[str, Any]] = []

    for src_key, (label, kind) in field_map.items():
        raw = source.get(src_key)
        if raw in (None, ""):
            continue
        value = (
            parse_br_percent_fraction(raw) if kind == "percent" else parse_br_float(raw)
        )
        indicators.append(
            {"key": src_key, "label": label, "kind": kind, "value": value}
        )

    for key in HISTORICAL_VARIATION_KEYS:
        raw = source.get(key)
        if raw in (None, ""):
            continue
        indicators.append(
            {
                "key": key,
                "label": key,
                "kind": "percent",
                "value": parse_br_percent_fraction(raw),
            }
        )

    for src_key, label in balance_map.items():
        raw = source.get(src_key)
        if raw in (None, ""):
            continue
        indicators.append(
            {
                "key": src_key,
                "label": label,
                "kind": "number",
                "value": parse_br_float(raw),
            }
        )

    return indicators


def get_fundamental_value(fundamentals: list[dict[str, Any]], key: str) -> Any:
    """
    Busca o 'value' de um indicador pela 'key' dentro da lista de fundamentals.
    Ex: get_fundamental_value(asset["fundamentals"], "Receita Líquida") -> 169530000000.0
    Retorna None se a key não existir na lista (indicador ausente na origem).
    """
    return next((item["value"] for item in fundamentals if item["key"] == key), None)


# ---------------------------------------------------------------------------
# Adapter principal
# ---------------------------------------------------------------------------


def extract_ticker_and_sub_type(
    source: dict[str, Any],
) -> tuple[Optional[str], Optional[str]]:
    """
    A origem manda a ação em 'Papel' (ex: 'PETR4') e o FII em 'FII' (ex: 'VGHF11').
    Retorna (ticker, subType) — subType='fii' só quando veio pela chave 'FII'.
    """
    fii = source.get("FII")
    if fii:
        return fii, "fii"

    papel = source.get("Papel")
    if papel:
        return papel, None

    return None, None


def infer_asset_type(sub_type: Optional[str]) -> str:
    return "fund" if sub_type == "fii" else "stock"


def build_meta(source: dict[str, Any], sub_type: Optional[str]) -> dict[str, Any]:
    """Campos complementares — o conjunto muda entre ação e FII pois a origem usa chaves diferentes."""
    if sub_type == "fii":
        return {
            "segment": source.get("Segmento"),
            "mandate": source.get("Mandato") or None,
            "management": source.get("Gestão"),
            "lastQuoteDate": parse_br_date(source.get("Data últ cot")),
            "lastReportDate": parse_br_date(source.get("Relatório")),
            "lastQuarterlyInfoDate": parse_br_date(source.get("Últ Info Trimestral")),
            "week52Low": parse_br_float(source.get("Min 52 sem")),
            "week52High": parse_br_float(source.get("Max 52 sem")),
            "quotaCount": parse_br_int(source.get("Nro. Cotas")),
        }
    return {
        "subsector": source.get("Subsetor"),
        "lastQuoteDate": parse_br_date(source.get("Data últ cot")),
        "lastBalanceDate": parse_br_date(source.get("Últ balanço processado")),
        "week52Low": parse_br_float(source.get("Min 52 sem")),
        "week52High": parse_br_float(source.get("Max 52 sem")),
        "shareCount": parse_br_int(source.get("Nro. Ações")),
        "enterpriseValue": parse_br_float(source.get("Valor da firma")),
    }


def build_asset(source: dict[str, Any]) -> dict[str, Any]:
    """Converte um dict bruto (retorno da sua API/scraper) para o objeto 'asset' normalizado."""

    papel, sub_type = extract_ticker_and_sub_type(source)

    return {
        "symbol": (papel or "").upper() or None,
        "name": source.get("Nome") if sub_type == "fii" else source.get("Empresa"),
        "assetType": infer_asset_type(sub_type),
        "provento": source.get("proventos"),
        "subType": sub_type,
        "currency": "BRL",
        "active": True,
        "exchange": "B3",
        "sector": source.get("Segmento") if sub_type == "fii" else source.get("Setor"),
        "logoUrl": None,
        "quote": {
            "lastPrice": parse_br_float(source.get("Cotação")),
            "changePercent": parse_br_percent(source.get("Dia")),
            "volume": parse_br_float(source.get("Vol $ méd (2m)")),
            "marketCap": parse_br_float(source.get("Valor de mercado")),
        },
        "fundamentals": build_fundamentals(source, sub_type),
        "variacaoDia": parse_br_percent_fraction(source.get("Dia")),
        "meta": build_meta(source, sub_type),
    }


def build_response(sources_list: list[dict[str, Any]]) -> dict[str, Any]:
    """Monta o payload final: {requestedAt, assets: [...]}."""
    return {
        "requestedAt": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "results": [build_asset(src) for src in sources_list],
    }


# if __name__ == "__main__":
#     import json

#     RAW_EXAMPLE = {
#         "Papel": "PETR4", "Cotação": "43,55", "Tipo": "PN",
#         "Data últ cot": "28/08/2026", "Empresa": "PETROBRAS PN",
#         "Min 52 sem": "27,19", "Setor": "Petróleo, Gás e Biocombustíveis",
#         "Max 52 sem": "47,35", "Subsetor": "Exploração, Refino e Distribuição",
#         "Vol $ méd (2m)": "1.607.350.000", "Valor de mercado": "561.304.000.000",
#         "Últ balanço processado": "30/06/2026", "Valor da firma": "874.073.000.000",
#         "Nro. Ações": "12.888.700.000", "Dia": "1,99%", "P/L": "4,21", "LPA": "10,35",
#         "Mês": "4,48%", "P/VP": "1,17", "VPA": "37,32", "30 dias": "9,15%",
#         "P/EBIT": "2,39", "Marg. Bruta": "50,6%", "12 meses": "53,88%", "PSR": "1,02",
#         "Marg. EBIT": "42,8%", "2026": "48,92%", "P/Ativos": "0,44",
#         "Marg. Líquida": "24,4%", "2025": "-6,06%", "P/Cap. Giro": "-21,26",
#         "EBIT / Ativo": "18,3%", "2024": "18,02%", "P/Ativ Circ Liq": "-0,87",
#         "ROIC": "19,7%", "2023": "94,43%", "Div. Yield": "8,4%", "ROE": "27,7%",
#         "2022": "46,00%", "EV / EBITDA": "2,71", "Liquidez Corr": "0,85",
#         "2021": "20,34%", "EV / EBIT": "3,73", "Dív Líq / Patrim": "0,65",
#         "Cres. Rec (5a)": "-2,3%", "Giro Ativos": "0,43", "Ativo": "1.279.020.000.000",
#         "Dív. Bruta": "366.533.000.000", "Disponibilidades": "53.764.000.000",
#         "Dív. Líquida": "312.769.000.000", "Ativo Circulante": "154.971.000.000",
#         "Patrim. Líq": "480.950.000.000", "Receita Líquida": "169.530.000.000",
#         "EBIT": "86.683.000.000", "Lucro Líquido": "52.445.000.000",
#     }

#     print(json.dumps(build_response([RAW_EXAMPLE]), indent=2, ensure_ascii=False))
