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
from app.utils.security import verify_api_key

logger = logging.getLogger("uvicorn.error")

CacheKey = tuple[str, int]

# ============================================================
# RECURSOS GLOBAIS
# ============================================================

scrape_semaphore = asyncio.Semaphore(settings.max_concurrent_scrapes)
browser_manager = BrowserManager()

cache: dict[CacheKey, tuple[float, NewsResponse]] = {}

# Quando uma fonte pode voltar a ser consultada
blocked_until: dict[CacheKey, float] = {}

# Um refresh em background por chave
background_tasks: dict[CacheKey, asyncio.Task] = {}

# Um scrape em andamento por chave (evita N requests iguais raspando juntas)
key_locks: dict[CacheKey, asyncio.Lock] = {}

# ============================================================
# CONFIGURAÇÃO
# ============================================================

BACKGROUND_FIRST_RETRY = 0.5
BACKGROUND_BACKOFF = [5]
BACKGROUND_MAX_TIME = 60

# Tempo máximo de um scrape (segura o semaphore, então precisa ter limite)
SCRAPE_TIMEOUT = 45


# ============================================================
# LIFESPAN
# ============================================================


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("[APP] Iniciando aplicação")
    try:
        yield
    finally:
        logger.info("[APP] Encerrando aplicação")

        tasks = list(background_tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        background_tasks.clear()
        await browser_manager.close()

        logger.info("[APP] Aplicação encerrada")


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="API para coleta de notícias com Playwright e cache com retry em background.",
    lifespan=lifespan,
    # dependencies=[Depends(verify_api_key)],
)


# ============================================================
# HELPERS
# ============================================================


def now() -> float:
    return time.monotonic()


def get_scraper(source: str):
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
    if cached is None:
        return 0
    return int(now() - cached[0])


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
    if items is None:
        raise ScraperError("Scraper não retornou resultado")

    items = list(items)
    if not items:
        raise ScraperError("Scraper retornou lista vazia")

    return NewsResponse(
        source=scraper.source_name,
        fetched_at=datetime.now(timezone.utc),
        count=len(items),
        stale=False,
        age_seconds=0,
        items=items,
    )


async def scrape_items(scraper, limit: int, *start_args, **start_kwargs):
    """Sobe o browser e executa o scrape sob semaphore e timeout."""
    async with scrape_semaphore:
        logger.info(
            "[Scrape] Semaphore adquirido (ativos: %s)",
            settings.max_concurrent_scrapes - scrape_semaphore._value,
        )
        key = await browser_manager.start(**start_kwargs)
        page = await browser_manager.new_page(key)
        try:
            return await asyncio.wait_for(
                scraper.scrape(page, limit),
                timeout=SCRAPE_TIMEOUT,
            )

        except (asyncio.TimeoutError, ScraperError) as exc:
            # Tenta fechar a página (se ela existir)
            if "page" in locals() and not page.is_closed():
                await page.close()
            # Força o fechamento do contexto para limpar conexões pendentes
            if key in browser_manager._contexts:
                await browser_manager.close_context(key)
            raise ScraperError(f"Scrape falhou: {exc}") from exc
        except asyncio.TimeoutError as exc:
            raise ScraperError(f"Scrape excedeu {SCRAPE_TIMEOUT}s") from exc
        finally:
            logger.info(
                "[Scrape] Semaphore liberando (ativos restantes: %s)",
                settings.max_concurrent_scrapes - scrape_semaphore._value - 1,
            )
            try:
                await asyncio.wait_for(page.close(), timeout=5)
            except Exception as e:
                logger.warning(f"[Scrape] page.close() falhou ou demorou: {e}")


async def scrape_news(scraper, limit: int, data_dir, useProxy) -> NewsResponse:
    items = await scrape_items(
        scraper,
        limit,
        data_dir=data_dir,
        useProxy=useProxy,
    )
    return build_result(scraper, items)


# ============================================================
# BACKGROUND REFRESH
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
                logger.warning(
                    "[NEWS] BACKGROUND atingiu limite de tempo: source=%s", source_key
                )
                break

            logger.info("[NEWS] BACKGROUND tentativa=%s source=%s", attempt, source_key)

            try:
                # Mantém o comportamento original: sem useProxy=False aqui
                result = await scrape_news(
                    scraper, limit, data_dir=data_dir, useProxy=useProxy
                )
            except Exception as exc:  # CancelledError não é capturado aqui
                logger.warning(
                    "[NEWS] BACKGROUND falhou: source=%s tentativa=%s erro=%s",
                    source_key,
                    attempt,
                    exc,
                )

                if is_block_error(exc):
                    blocked_until[key] = now() + settings.blocked_cooldown_seconds
                    logger.warning(
                        "[NEWS] Bloqueio detectado. Cooldown=%ss source=%s",
                        settings.blocked_cooldown_seconds,
                        source_key,
                    )
                    break

                # Não dorme depois da última tentativa
                if attempt == len(BACKGROUND_BACKOFF):
                    break

                logger.info("[NEWS] BACKGROUND aguardando %ss", delay)
                await asyncio.sleep(delay)
                continue

            store_result(key, result)
            logger.info(
                "[NEWS] BACKGROUND SUCESSO: source=%s items=%s tentativa=%s",
                source_key,
                result.count,
                attempt,
            )
            return

        logger.error("[NEWS] BACKGROUND terminou sem sucesso: source=%s", source_key)

    except asyncio.CancelledError:
        logger.info("[NEWS] BACKGROUND cancelado: source=%s", source_key)
        raise

    except Exception:
        logger.exception("[NEWS] Erro inesperado no BACKGROUND: source=%s", source_key)

    finally:
        # Só remove se a entrada ainda for esta task
        if background_tasks.get(key) is asyncio.current_task():
            background_tasks.pop(key, None)
        logger.info("[NEWS] BACKGROUND finalizado: source=%s", source_key)


def start_background_refresh(key, scraper, limit, data_dir, useProxy) -> None:
    existing = background_tasks.get(key)
    if existing is not None and not existing.done():
        logger.info("[NEWS] BACKGROUND já está rodando: %s", key)
        return

    background_tasks[key] = asyncio.create_task(
        background_refresh(
            key=key, scraper=scraper, limit=limit, data_dir=data_dir, useProxy=useProxy
        )
    )
    logger.info("[NEWS] BACKGROUND agendado: %s", key)


# ============================================================
# HEALTH
# ============================================================

public_router = APIRouter()


@public_router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", version=settings.app_version)


app.include_router(public_router)


# ============================================================
# TESTE
# ============================================================


@app.get("/teste")
async def teste(
    source: Annotated[str, Query(min_length=1, max_length=30)] = "teste",
    ticker: str = Query("PETR4", min_length=1),
):
    scraper = get_scraper(source)
    search = require_method(scraper, "search")

    try:
        return await search(ticker.upper())
    except ScraperError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {"items": "items"}


# ============================================================
# NEWS
# ============================================================


@app.get("/news", response_model=NewsResponse)
async def news(
    response: Response,
    source: str = Query("investing", min_length=1, max_length=30),
    limit: int = Query(20, ge=1, le=settings.max_news_items),
    proxy: bool = True,
) -> NewsResponse:
    source_key = source.lower()
    scraper = get_scraper(source_key)
    key: CacheKey = (source_key, limit)

    # 1. CACHE FRESCO
    cached, is_fresh = get_cache(key)

    if cached is not None and is_fresh:
        logger.info("[NEWS] CACHE HIT: source=%s limit=%s", source_key, limit)
        return hit_response(response, key, cached)

    # 2. CACHE STALE: devolve na hora e atualiza em background
    if cached is not None:
        remaining = cooldown_remaining(key)

        if remaining:
            logger.info(
                "[NEWS] Cache stale, fonte em cooldown: source=%s restante=%ss",
                source_key,
                remaining,
            )
        else:
            logger.info("[NEWS] CACHE STALE: source=%s limit=%s", source_key, limit)
            start_background_refresh(
                key, scraper, limit, data_dir=source_key, useProxy=proxy
            )

        return stale_response(response, key, cached)

    # 3. SEM CACHE + COOLDOWN
    remaining = cooldown_remaining(key)

    if remaining:
        logger.warning(
            "[NEWS] Fonte em cooldown: source=%s restante=%ss", source_key, remaining
        )
        raise HTTPException(
            status_code=503,
            detail="A fonte está temporariamente indisponível. Tente novamente em instantes.",
            headers={"Retry-After": str(min(remaining, 60))},
        )

    # 4. CACHE MISS: um scrape por chave
    logger.info("[NEWS] CACHE MISS: source=%s limit=%s", source_key, limit)

    lock = key_locks.setdefault(key, asyncio.Lock())

    async with lock:
        # Outra request pode ter preenchido o cache enquanto esperávamos
        cached, is_fresh = get_cache(key)
        if cached is not None and is_fresh:
            logger.info("[NEWS] CACHE preenchido enquanto aguardava")
            return hit_response(response, key, cached)

        try:
            logger.info(
                "[NEWS] Fazendo scrape inicial: source=%s limit=%s", source_key, limit
            )
            result = await scrape_news(
                scraper, limit, data_dir=source_key, useProxy=proxy
            )

        except Exception as exc:  # CancelledError passa direto
            blocked = is_block_error(exc)

            if isinstance(exc, ScraperError):
                logger.warning(
                    "[NEWS] Scrape inicial falhou: source=%s erro=%s", source_key, exc
                )
            else:
                logger.exception("[NEWS] Erro inesperado: source=%s", source_key)

            if blocked:
                await browser_manager.close_context(key)
                blocked_until[key] = now() + settings.blocked_cooldown_seconds
                start_background_refresh(
                    key, scraper, limit, data_dir=source_key, useProxy=proxy
                )

            cached, _ = get_cache(key)
            if cached is not None:
                return stale_response(response, key, cached)

            if blocked:
                raise HTTPException(
                    status_code=503,
                    detail="A fonte recusou temporariamente a consulta. Tente novamente em até 1 minuto.",
                    headers={"Retry-After": "60"},
                ) from exc

            status = 502 if isinstance(exc, ScraperError) else 503
            raise HTTPException(status_code=status, detail=str(exc)) from exc

        store_result(key, result)

        logger.info(
            "[NEWS] Cache atualizado: source=%s limit=%s items=%s",
            source_key,
            limit,
            result.count,
        )

        set_cache_headers(response, "MISS", 0)
        return result


# ============================================================
# FUNDAMENTUS
# ============================================================


@app.get("/tickers")
async def searchTicket(
    source: str = Query("fundamentus", min_length=1, max_length=30),
    ticker: str = Query("PETR4", min_length=1),
):
    scraper = get_scraper(source)

    search = require_method(scraper, "search")
    # search = require_method(scraper, "searchFundamentusHttpx")

    try:
        return await search(ticker.upper())
    except ScraperError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/tickers/historical")
async def fundamentus_historical(
    source: str = Query(
        "fundamentus",
        min_length=1,
        max_length=30,
        description="Fonte dos dados (fundamentus, yahoo, etc)",
    ),
    ticker: str = Query(
        "PETR4", min_length=1, description="Código do ativo (ex: PETR4, VALE3)"
    ),
    range_: str = Query(
        "1mo",
        alias="range",
        min_length=1,
        max_length=10,
        description="Período total dos dados (1mo, 6mo, 1y, etc)",
    ),
    interval: str = Query(
        "1mo",
        min_length=1,
        max_length=10,
        description="Intervalo entre os pontos (1d, 1wk, 1mo, etc)",
    ),
    order: str = Query(
        "asc",
        min_length=3,
        max_length=4,
        description="Ordem de classificação (asc ou desc)",
    ),
):
    order = order.lower()
    if order not in ("asc", "desc"):
        raise HTTPException(
            status_code=400, detail="O parâmetro 'order' deve ser 'asc' ou 'desc'"
        )

    scraper = get_scraper(source)
    historical = require_method(scraper, "historicalFundamentusHttpx")

    try:
        return await historical(ticker.upper(), range_, order == "desc")
    except ScraperError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/tickers/fundamentals")
async def fundamentals(
    source: str = Query("investidor10", min_length=1, max_length=30),
    kind: str = Query(min_length=1),
    url: str = Query(min_length=1),
):
    if not url:
        raise HTTPException(
            status_code=503,
            detail=("A url deve ser informado."),
        )
    scraper = get_scraper(source)
    fundamentals = require_method(scraper, "fundamentals")
    try:
        return await fundamentals(url, kind)
    except ScraperError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# ============================================================
# SOURCES
# ============================================================


@app.get("/sources")
async def sources() -> dict[str, list[str]]:
    return {"sources": sorted(SCRAPERS)}
