import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Response

from app.browser import BrowserManager
from app.config import settings
from app.scrapers import SCRAPERS
from app.scrapers.base import ScraperError
from app.utils.models import HealthResponse, NewsResponse

logger = logging.getLogger("uvicorn.error")

CacheKey = tuple[str, int]

# ============================================================
# CONTROLE DE CONCORRÊNCIA E RECURSOS GLOBAIS
# ============================================================

# 1. Semáforo estrito para o uso do Playwright/Navegador (Evita 'Exceeded concurrency limit')
browser_semaphore = asyncio.Semaphore(settings.max_concurrent_scrapes)

# 2. Semáforo amplo para chamadas HTTP puras (Fundamentus, Tesouro)
http_semaphore = asyncio.Semaphore(20)

browser_manager = BrowserManager()
cache: dict[CacheKey, tuple[float, NewsResponse]] = {}
blocked_until: dict[CacheKey, float] = {}
background_tasks: dict[CacheKey, asyncio.Task] = {}
key_locks: dict[CacheKey, asyncio.Lock] = {}

BACKGROUND_FIRST_RETRY = 0.5
BACKGROUND_BACKOFF = [5]
BACKGROUND_MAX_TIME = 60
SCRAPE_TIMEOUT = 45


# ============================================================
# LIFESPAN DA APLICAÇÃO
# ============================================================


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Gerencia a inicialização e o encerramento gracioso dos recursos."""
    logger.info("[APP] Iniciando aplicação")
    try:
        yield
    finally:
        logger.info("[APP] Encerrando aplicação")

        # Cancela tasks de background pendentes
        tasks = list(background_tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        background_tasks.clear()

        # Encerra o gerenciador de navegadores
        await browser_manager.close()
        logger.info("[APP] Aplicação encerrada com sucesso")


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="API otimizada de scraping financeiro com suporte a Playwright, HTTP assíncrono e cache inteligente.",
    lifespan=lifespan,
)


# ============================================================
# FUNÇÕES AUXILIARES / CACHE
# ============================================================


def now() -> float:
    return time.monotonic()


def get_scraper(source: str):
    """Obtém a classe do scraper registrada pelo seu nome."""
    scraper = SCRAPERS.get(source.lower())
    if scraper is None:
        raise HTTPException(
            status_code=404,
            detail={
                "message": "Fonte não cadastrada",
                "available_sources": sorted(SCRAPERS),
            },
        )
    return scraper


def require_method(scraper, name: str):
    """Valida se o scraper possui o método solicitado."""
    method = getattr(scraper, name, None)
    if method is None:
        raise HTTPException(
            status_code=400,
            detail=f"A fonte '{scraper.source_name}' não suporta esta operação",
        )
    return method


def get_cache(key: CacheKey) -> tuple[NewsResponse | None, bool]:
    cached = cache.get(key)
    if cached is None:
        return None, False

    timestamp, response = cached
    is_fresh = (now() - timestamp) < settings.cache_ttl_seconds
    return response, is_fresh


def cache_age_seconds(key: CacheKey) -> int:
    cached = cache.get(key)
    return int(now() - cached[0]) if cached else 0


def cooldown_remaining(key: CacheKey) -> int:
    remaining = blocked_until.get(key, 0.0) - now()
    return int(remaining) + 1 if remaining > 0 else 0


def set_cache_headers(response: Response, state: str, age: int) -> None:
    response.headers["X-Cache"] = state
    response.headers["X-Cache-Age"] = str(age)
    response.headers["Cache-Control"] = "no-store"


def hit_response(response: Response, key: CacheKey, cached: NewsResponse):
    set_cache_headers(response, "HIT", cache_age_seconds(key))
    return cached


def stale_response(response: Response, key: CacheKey, cached: NewsResponse):
    age = cache_age_seconds(key)
    set_cache_headers(response, "STALE", age)
    return cached.model_copy(update={"stale": True, "age_seconds": age})


def is_block_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(
        marker in message
        for marker in ("http 403", "status=403", "http 407", "status=407")
    )


def store_result(key: CacheKey, result: NewsResponse) -> None:
    cache[key] = (now(), result)
    blocked_until.pop(key, None)


def build_result(scraper, items) -> NewsResponse:
    if not items:
        raise ScraperError("Scraper retornou lista vazia ou nula")

    return NewsResponse(
        source=scraper.source_name,
        fetched_at=datetime.now(timezone.utc),
        count=len(items),
        stale=False,
        age_seconds=0,
        items=list(items),
    )


async def scrape_items(scraper, limit: int, *start_args, **start_kwargs):
    """Executa a raspagem com limite de navegador, timeout e limpeza garantida."""
    async with browser_semaphore:
        active = settings.max_concurrent_scrapes - browser_semaphore._value
        logger.info("[Scrape] Browser Semaphore adquirido (ativos: %s)", active)

        key = await browser_manager.start(**start_kwargs)
        page = None

        try:
            try:
                page = await browser_manager.new_page(key)
            except Exception as exc:
                logger.warning(
                    "[Scrape] Contexto %s inválido ao abrir página; recriando: %s",
                    key,
                    exc,
                )
                await browser_manager.close_context(key)
                key = await browser_manager.start(**start_kwargs)
                page = await browser_manager.new_page(key)

            return await asyncio.wait_for(
                scraper.scrape(page, limit),
                timeout=SCRAPE_TIMEOUT,
            )

        except asyncio.TimeoutError as exc:
            await browser_manager.close_context(key)
            raise ScraperError(
                f"Scrape excedeu tempo limite de {SCRAPE_TIMEOUT}s"
            ) from exc

        except asyncio.CancelledError:
            logger.warning("[Scrape] Tarefa cancelada; liberando recursos")
            raise

        finally:
            # Scrapers podem fechar a página por conta própria; is_closed evita
            # uma segunda tentativa. Também cobre exceções inesperadas.
            if page is not None:
                try:
                    if not page.is_closed():
                        await page.close()
                except Exception:
                    logger.exception("[Scrape] Falha ao fechar página")


async def scrape_news(scraper, limit: int, data_dir, useProxy) -> NewsResponse:
    items = await scrape_items(
        scraper,
        limit,
        data_dir=data_dir,
        useProxy=useProxy,
    )
    return build_result(scraper, items)


# ============================================================
# BACKGROUND REFRESH DE CACHE
# ============================================================


async def background_refresh(
    key: CacheKey, scraper, limit: int, data_dir: str, useProxy: bool
) -> None:
    source_key = key[0]
    started_at = now()
    logger.info("[NEWS] BACKGROUND iniciado: source=%s limit=%s", source_key, limit)

    try:
        await asyncio.sleep(BACKGROUND_FIRST_RETRY)
        for attempt, delay in enumerate(BACKGROUND_BACKOFF, start=1):
            if now() - started_at >= BACKGROUND_MAX_TIME:
                break

            try:
                result = await scrape_news(
                    scraper, limit, data_dir=data_dir, useProxy=useProxy
                )
                store_result(key, result)
                logger.info(
                    "[NEWS] BACKGROUND SUCESSO: source=%s items=%s",
                    source_key,
                    result.count,
                )
                return
            except Exception as exc:
                if is_block_error(exc):
                    blocked_until[key] = now() + settings.blocked_cooldown_seconds
                    break
                if attempt < len(BACKGROUND_BACKOFF):
                    await asyncio.sleep(delay)
    except asyncio.CancelledError:
        raise
    finally:
        if background_tasks.get(key) is asyncio.current_task():
            background_tasks.pop(key, None)


def start_background_refresh(key, scraper, limit, data_dir, useProxy) -> None:
    existing = background_tasks.get(key)
    if existing is not None and not existing.done():
        return

    background_tasks[key] = asyncio.create_task(
        background_refresh(
            key=key, scraper=scraper, limit=limit, data_dir=data_dir, useProxy=useProxy
        )
    )


# ============================================================
# ROTAS / ENDPOINTS DA API
# ============================================================

public_router = APIRouter()


@public_router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", version=settings.app_version)


app.include_router(public_router)


@app.get("/tesouro/indicators")
async def indicators(
    symbols: str = Query(..., min_length=1),
    source: Annotated[str, Query(min_length=1, max_length=30)] = "tesouro",
):
    scraper = get_scraper(source)
    indicators_func = require_method(scraper, "indicators")
    lst = symbols.split(",")
    if len(lst) > 50:
        raise HTTPException(400, "Máximo de 50 símbolos por requisição")

    # Protegido por semáforo HTTP
    async with http_semaphore:
        return await indicators_func(lst)


@app.get("/tesouro/search")
async def search(
    q: str | None = Query(default=None),
    source: Annotated[str, Query(min_length=1, max_length=30)] = "tesouro",
):
    scraper = get_scraper(source)
    search_func = require_method(scraper, "search")
    try:
        async with http_semaphore:
            return await search_func(q)
    except ScraperError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/news", response_model=NewsResponse)
async def news(
    response: Response,
    source: str = Query("broadcast", min_length=1, max_length=30),
    limit: int = Query(20, ge=1, le=settings.max_news_items),
    proxy: bool = True,
) -> NewsResponse:
    source_key = source.lower()
    scraper = get_scraper(source_key)
    key: CacheKey = (source_key, limit)

    # 1. CACHE FRESCO
    cached, is_fresh = get_cache(key)
    if cached is not None and is_fresh:
        return hit_response(response, key, cached)

    # 2. CACHE STALE
    if cached is not None:
        if not cooldown_remaining(key):
            start_background_refresh(
                key, scraper, limit, data_dir=source_key, useProxy=proxy
            )
        return stale_response(response, key, cached)

    # 3. VERIFICA COOLDOWN
    remaining = cooldown_remaining(key)
    if remaining:
        raise HTTPException(
            status_code=503,
            detail="A fonte está temporariamente indisponível. Tente novamente em instantes.",
            headers={"Retry-After": str(min(remaining, 60))},
        )

    # 4. CACHE MISS
    lock = key_locks.setdefault(key, asyncio.Lock())
    async with lock:
        cached, is_fresh = get_cache(key)
        if cached is not None and is_fresh:
            return hit_response(response, key, cached)

        try:
            result = await scrape_news(
                scraper, limit, data_dir=source_key, useProxy=proxy
            )
        except Exception as exc:
            blocked = is_block_error(exc)
            if blocked:
                await browser_manager.close_context(key)
                blocked_until[key] = now() + settings.blocked_cooldown_seconds
                start_background_refresh(
                    key, scraper, limit, data_dir=source_key, useProxy=proxy
                )

            cached, _ = get_cache(key)
            if cached is not None:
                return stale_response(response, key, cached)

            status = 502 if isinstance(exc, ScraperError) else 503
            raise HTTPException(status_code=status, detail=str(exc)) from exc

        store_result(key, result)
        set_cache_headers(response, "MISS", 0)
        return result


@app.get("/tickers")
async def searchTicket(
    source: str = Query("fundamentus", min_length=1, max_length=30),
    ticker: str = Query("PETR4", min_length=1),
):
    scraper = get_scraper(source)
    search_func = require_method(scraper, "search")
    try:
        async with http_semaphore:
            return await search_func(ticker.upper())
    except ScraperError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/tickers/historical")
async def fundamentus_historical(
    source: str = Query("fundamentus", min_length=1, max_length=30),
    ticker: str = Query("PETR4", min_length=1),
    range_: str = Query("1mo", alias="range", min_length=1, max_length=10),
    interval: str = Query("1mo", min_length=1, max_length=10),
    order: str = Query("asc", min_length=3, max_length=4),
):
    order_clean = order.lower()
    if order_clean not in ("asc", "desc"):
        raise HTTPException(
            status_code=400, detail="O parâmetro 'order' deve ser 'asc' ou 'desc'"
        )

    scraper = get_scraper(source)
    historical_func = require_method(scraper, "historicalFundamentusHttpx")

    try:
        async with http_semaphore:
            return await historical_func(ticker.upper(), range_, order_clean == "desc")
    except ScraperError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/tickers/fundamentals")
async def fundamentals(
    source: str = Query("investidor10", min_length=1, max_length=30),
    kind: str = Query(min_length=1),
    url: str = Query(min_length=1),
    ticker: str = Query("PETR4", min_length=1),
):
    if not url:
        raise HTTPException(status_code=400, detail="A url deve ser informada.")

    scraper = get_scraper(source)
    fundamentals_func = require_method(scraper, "fundamentals")
    try:
        async with http_semaphore:
            return await fundamentals_func(url, kind, ticker)
    except ScraperError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/sources")
async def sources() -> dict[str, list[str]]:
    return {"sources": sorted(SCRAPERS)}
