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
    "fii": "fii",
    "ETFs": "etf",
    "BDRs": "bdr",
    "Tesouro Direto": "treasury",
    "Renda Fixa": "fixed_income",
}


def parse_br_float(value: Optional[str]) -> Optional[float]:
    """Converte 'R$ 6,39 B' -> 6390000000.0 | 'R$ 25,94 M' -> 25940000.0 | '795,39 mil' -> 795390.0"""
    if value is None:
        return None
    s = str(value).strip()
    if s == "" or s.lower() in {"n/a", "-", "null"}:
        return None

    # 1. Identifica multiplicadores (Bilhão, Milhão, Mil)
    multiplicador = 1
    if " B" in s or "Bilhões" in s or "Bilhão" in s:
        multiplicador = 1_000_000_000
    elif " M" in s or "Milhões" in s or "Milhão" in s:
        multiplicador = 1_000_000
    elif " mil" in s.lower():
        multiplicador = 1_000

    # 2. Limpa a string para sobrar apenas o número
    s = s.replace("R$", "").replace("%", "")
    s = re.sub(r"[a-zA-Z\s]+", "", s).strip()

    # 3. Converte para float BR
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s) * multiplicador
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
    # --- Ações (mantidos) ---
    "Cotação": ("Cotação", "number"),
    "VARIAÇÃO (12M)": ("Variação (12M)", "percent"),
    "P/L": ("P/L", "number"),
    "P/VP": ("P/VP", "number"),
    "DY": (
        "Dividend Yield",
        "percent",
    ),  # No JSON veio 'DY', mapeamos para Dividend Yield
    "Dividend Yield": ("Dividend Yield", "percent"),  # Caso venha duplicado
    "P/Receita (PSR)": ("PSR", "number"),
    "EV/Ebitda": ("EV/EBITDA", "number"),
    "EV/Ebit": ("EV/EBIT", "number"),
    "P/Ebitda": ("P/EBITDA", "number"),
    "P/Ebit": ("P/EBIT", "number"),
    "P/Ativo": ("P/Ativos", "number"),
    "P/Ativo Circ. Liq.": ("P/Ativo Circulante Líquido", "number"),
    "P/Cap.Giro": ("P/Capital de Giro", "number"),
    "LPA": ("LPA", "number"),
    "VPA": ("VPA", "number"),
    "Margem Bruta": ("Margem Bruta", "percent"),
    "Margem Ebitda": ("Margem EBITDA", "percent"),
    "Margem Ebit": ("Margem EBIT", "percent"),
    "Margem Líquida": ("Margem Líquida", "percent"),
    "Giro Ativos": ("Giro de Ativos", "number"),
    "ROE": ("ROE", "percent"),
    "ROA": ("ROA", "percent"),
    "ROIC": ("ROIC", "percent"),
    "Payout": ("Payout", "percent"),
    "Liquidez Corrente": ("Liquidez Corrente", "number"),
    "Divida Liquida/Ebitda": ("Dívida Líquida / EBITDA", "number"),
    "Divida Liquida/Ebit": ("Dívida Líquida / EBIT", "number"),
    "Divida Liquida/Patrimônio": ("Dívida Líquida / Patrimônio", "number"),
    "Divida Bruta/Patrimônio": ("Dívida Bruta / Patrimônio", "number"),
    "Patrimônio/Ativos": ("Patrimônio / Ativos", "number"),
    "Passivos/Ativos": ("Passivos / Ativos", "number"),
    "CAGR Receitas 5 anos": ("Crescimento da Receita (5a)", "percent"),
    "CAGR Lucros 5 anos": ("Crescimento do Lucro (5a)", "percent"),
    # --- FIIs (novos campos do seu scraper) ---
    "Cotação": ("Cotação", "number"),
    "DY (12M)": ("Dividend Yield", "percent"),
    "Dividend Yield": ("Dividend Yield", "percent"),
    "P/VP": ("P/VP", "number"),  # Pode repetir, ele vai sobrescrever
    "Liquidez Diária": ("Liquidez Diária", "number"),
    "VARIAÇÃO (12M)": ("Variação (12M)", "percent"),
    "Valor de Mercado": ("Valor de Mercado", "number"),
    "Valor Patrimonial": ("Valor Patrimonial", "number"),
    "Val. Patrimonial p/ Cota": ("Valor Patrimonial por Cota", "number"),
    "Vacância": ("Vacância", "percent"),
    "Número de Cotistas": ("Número de Cotistas", "number"),
    "Cotas Emitidas": ("Cotas Emitidas", "number"),
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


def _limpar_chave(chave: str, ticker: str) -> str:
    # Remove o ticker da string (ex: "XPML11 Cotação" -> "Cotação")
    # O regex busca o ticker seguido de espaço no começo da string
    chave_limpa = re.sub(rf"^{ticker}\s+", "", chave, flags=re.IGNORECASE).strip()
    return chave_limpa


def build_fundamentals(source: dict[str, Any]) -> list[dict[str, Any]]:
    """Monta a lista de indicadores no formato [{key, label, kind, value}, ...].
    Usa o mapeamento de FII quando sub_type == 'fii', senão o de ação.
    """
    sub_type = source.get("sub_type")
    ticker = source.get("ticker", "")  # Pega o ticker (ex: XPML11)

    # --- NOVIDADE AQUI: Limpando as chaves do source ---
    # Isso transforma "XPML11 Cotação" em "Cotação" antes de buscar no mapeamento
    source_limpo = {}
    for k, v in source.items():
        # Só tenta limpar se o ticker não for vazio e se a chave começar com ele
        if ticker and k.startswith(ticker):
            chave_sem_ticker = _limpar_chave(k, ticker)
            source_limpo[chave_sem_ticker] = v
        else:
            source_limpo[k] = v
    # ---------------------------------------------------

    field_map = FUNDAMENTAL_FIELDS
    balance_map = FII_BALANCE_FIELDS if sub_type == "fii" else BALANCE_SHEET_FIELDS
    indicators: list[dict[str, Any]] = []

    for src_key, (label, kind) in field_map.items():
        # Agora buscamos no source_limpo!

        raw = source_limpo.get(src_key)
        if raw in (None, ""):
            continue
        value = (
            parse_br_percent_fraction(raw) if kind == "percent" else parse_br_float(raw)
        )
        indicators.append(
            {"key": src_key, "label": label, "kind": kind, "value": value}
        )

    return indicators


def build_asset_completo(source: dict[str, Any]):
    # Pega os dados básicos (ticker, nome, etc)
    asset = build_asset(source)

    # Pega a lista de fundamentos
    fundamentos = build_fundamentals(source)

    # Adiciona a lista de fundamentos dentro do objeto asset
    asset["fundamentals"] = fundamentos

    return asset


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
            "lastPrice": parse_br_float(
                source.get("currency") or source.get("Cotação")
            ),
            "changePercent": parse_br_percent(
                source.get("changePercent") or source.get("VARIAÇÃO (12M)")
            ),
            "period": source.get("period") or "12M",
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


def build_response_fundamentals(sources_list: list[dict[str, Any]]) -> dict[str, Any]:
    """Monta o payload final: {requestedAt, assets: [...]}."""
    return {
        "requestedAt": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "results": [build_asset_completo(src) for src in sources_list],
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
    raw = source.get("assetType") or source.get("sub_type")

    if raw:
        return ASSET_TYPE_MAP.get(raw, raw)

    return None
