import asyncio
import http
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from app.utils.security import verify_api_key
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Response
from typing import Annotated


from app.browser import BrowserManager
from app.config import settings
from app.utils.models import HealthResponse, NewsResponse
from app.scrapers import SCRAPERS
from app.scrapers.base import ScraperError

logger = logging.getLogger("uvicorn.error")

# ============================================================
# RECURSOS GLOBAIS
# ============================================================

scrape_semaphore = asyncio.Semaphore(settings.max_concurrent_scrapes)
browser_manager = BrowserManager()
cache: dict[tuple[str, int], tuple[float, NewsResponse]] = {}

# Controla quando uma fonte pode voltar a ser consultada
blocked_until: dict[tuple[str, int], float] = {}

# Controla os refreshes em background
#
# Evita:
#
# request 1 -> background
# request 2 -> background
# request 3 -> background
#
# todos fazendo scrape ao mesmo tempo.
background_tasks: dict[tuple[str, int], asyncio.Task] = {}


# ============================================================
# CONFIGURAÇÃO DO RETRY
# ============================================================

# Primeira tentativa do background
BACKGROUND_FIRST_RETRY = 0.5

# Backoff entre tentativas
BACKGROUND_BACKOFF = [5, 10, 20, 30]

# Tempo máximo aproximado de recuperação
BACKGROUND_MAX_TIME = 60


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

        # Cancela os refreshes em background
        for key, task in list(background_tasks.items()):

            if not task.done():

                logger.info(
                    "[APP] Cancelando background refresh: %s",
                    key,
                )

                task.cancel()

        if background_tasks:

            await asyncio.gather(
                *background_tasks.values(),
                return_exceptions=True,
            )

        background_tasks.clear()

        await browser_manager.close()

        logger.info("[APP] Aplicação encerrada")


# ============================================================
# FASTAPI
# ============================================================


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=(
        "API para coleta de notícias com Playwright " "e cache com retry em background."
    ),
    lifespan=lifespan,
    # dependencies=[Depends(verify_api_key)],
)


# ============================================================
# FUNÇÃO PARA VERIFICAR CACHE
# ============================================================
def cache_age_seconds(cache_key: tuple[str, int]) -> int | None:
    cached = cache.get(cache_key)
    if cached is None:
        return None

    timestamp, _ = cached

    now = asyncio.get_running_loop().time()
    return int(now - timestamp)


def get_cache(
    cache_key: tuple[str, int],
) -> tuple[NewsResponse | None, bool]:

    cached = cache.get(cache_key)

    if cached is None:

        return None, False

    timestamp, response = cached

    now = asyncio.get_running_loop().time()

    age = now - timestamp

    is_fresh = age < settings.cache_ttl_seconds

    return response, is_fresh


# ============================================================
# FUNÇÃO PARA IDENTIFICAR BLOQUEIO
# ============================================================


def is_block_error(exc: Exception) -> bool:

    message = str(exc).lower()

    return (
        "http 403" in message
        or "status=403" in message
        or "http 407" in message
        or "status=407" in message
        or "proxy" in message
        and "407" in message
    )


# ============================================================
# BACKGROUND REFRESH
# ============================================================


async def background_refresh(
    cache_key: tuple[str, int],
    scraper,
    limit: int,
) -> None:

    source_key = cache_key[0]

    logger.warning(
        "[NEWS] BACKGROUND iniciado: source=%s limit=%s",
        source_key,
        limit,
    )

    started_at = asyncio.get_running_loop().time()

    try:

        # ------------------------------------------------------
        # Primeira espera
        # ------------------------------------------------------

        logger.info(
            "[NEWS] BACKGROUND aguardando %ss antes da tentativa",
            BACKGROUND_FIRST_RETRY,
        )

        await asyncio.sleep(BACKGROUND_FIRST_RETRY)

        attempt = 0

        for delay in BACKGROUND_BACKOFF:

            elapsed = asyncio.get_running_loop().time() - started_at

            if elapsed >= BACKGROUND_MAX_TIME:

                logger.warning(
                    "[NEWS] BACKGROUND atingiu limite de tempo: " "source=%s",
                    source_key,
                )

                break

            attempt += 1

            logger.info(
                "[NEWS] BACKGROUND tentativa=%s source=%s",
                attempt,
                source_key,
            )

            # --------------------------------------------------
            # Semaphore somente durante o SCRAPE.
            #
            # NÃO seguramos semaphore durante sleep.
            # --------------------------------------------------

            try:

                async with scrape_semaphore:
                    await browser_manager.start()
                    if browser_manager.context is None:
                        raise ScraperError("Contexto Playwright não foi criado")
                    logger.info(
                        "[NEWS] BACKGROUND fazendo scrape: " "source=%s tentativa=%s",
                        source_key,
                        attempt,
                    )
                    items = await scraper.scrape(
                        browser_manager,
                        limit,
                    )

                # --------------------------------------------------
                # Resultado válido
                # --------------------------------------------------

                if items is None:

                    raise ScraperError("Scraper não retornou resultado")

                items = list(items)

                if not items:

                    raise ScraperError("Scraper retornou lista vazia")

                result = NewsResponse(
                    source=scraper.source_name,
                    fetched_at=datetime.now(timezone.utc),
                    count=len(items),
                    stale=False,
                    age_seconds=0,
                    items=items,
                )

                cache[cache_key] = (
                    asyncio.get_running_loop().time(),
                    result,
                )

                blocked_until.pop(
                    cache_key,
                    None,
                )

                logger.info(
                    "[NEWS] BACKGROUND SUCESSO: " "source=%s items=%s tentativa=%s",
                    source_key,
                    len(items),
                    attempt,
                )

                return

            except asyncio.CancelledError:

                logger.info(
                    "[NEWS] BACKGROUND cancelado: source=%s",
                    source_key,
                )

                raise

            except Exception as exc:

                logger.warning(
                    "[NEWS] BACKGROUND falhou: " "source=%s tentativa=%s erro=%s",
                    source_key,
                    attempt,
                    exc,
                )

                # --------------------------------------------------
                # Se for 403/407, marca cooldown.
                # --------------------------------------------------

                if is_block_error(exc):
                    cooldown = settings.blocked_cooldown_seconds

                    blocked_until[cache_key] = (
                        asyncio.get_running_loop().time() + cooldown
                    )

                    logger.warning(
                        "[NEWS] Bloqueio detectado. "
                        "Não haverá novas tentativas imediatas. "
                        "Cooldown=%ss source=%s",
                        cooldown,
                        source_key,
                    )

                    break

                # --------------------------------------------------
                # Se ainda houver tempo, espera antes da próxima.
                # --------------------------------------------------

                elapsed = asyncio.get_running_loop().time() - started_at

                if elapsed >= BACKGROUND_MAX_TIME:

                    break

                logger.info(
                    "[NEWS] BACKGROUND aguardando %ss " "antes da próxima tentativa",
                    delay,
                )

                await asyncio.sleep(delay)

        logger.error(
            "[NEWS] BACKGROUND terminou sem sucesso: " "source=%s",
            source_key,
        )

    except asyncio.CancelledError:

        logger.info(
            "[NEWS] BACKGROUND cancelado: source=%s",
            source_key,
        )

        raise

    except Exception:

        logger.exception(
            "[NEWS] Erro inesperado no BACKGROUND: source=%s",
            source_key,
        )

    finally:

        background_tasks.pop(
            cache_key,
            None,
        )

        logger.info(
            "[NEWS] BACKGROUND finalizado: source=%s",
            source_key,
        )


# ============================================================
# INICIA BACKGROUND REFRESH
# ============================================================


def start_background_refresh(
    cache_key: tuple[str, int],
    scraper,
    limit: int,
) -> None:

    existing = background_tasks.get(cache_key)

    # Já existe refresh rodando
    if existing is not None:

        if not existing.done():

            logger.info(
                "[NEWS] BACKGROUND já está rodando: %s",
                cache_key,
            )

            return

    task = asyncio.create_task(
        background_refresh(
            cache_key=cache_key,
            scraper=scraper,
            limit=limit,
        )
    )

    background_tasks[cache_key] = task

    logger.info(
        "[NEWS] BACKGROUND agendado: %s",
        cache_key,
    )


# ============================================================
# HEALTH
# ============================================================

public_router = APIRouter()


@public_router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        version=settings.app_version,
    )


app.include_router(public_router)


# ============================================================
# TESTE
# ============================================================
@app.get("/teste")
async def teste(
    source: Annotated[str, Query(min_length=1, max_length=30)] = "statusinvest",
    ticker: str = Query(
        "PETR4",
        min_length=1,
    ),
):
    source_key = source.lower()

    scraper = SCRAPERS.get(source_key)
    if scraper is None:
        raise HTTPException(
            status_code=404,
            detail={
                "message": "Fonte não cadastrada",
                "available_sources": sorted(SCRAPERS),
            },
        )
    try:
        await browser_manager.start(useProxy=False)
        if browser_manager.context is None:
            raise ScraperError("Contexto Playwright não foi criado")

        dados = await scraper.scrape(browser_manager.context, ticker)

        return {dados}
    except asyncio.CancelledError:
        raise
    except ScraperError as exc:
        raise
    # finally:
    # if page:
    #     await page.close()


# ============================================================
# NEWS
# ============================================================


@app.get(
    "/news",
    response_model=NewsResponse,
)
async def news(
    response: Response,
    source: str = Query(
        "investing",
        min_length=1,
        max_length=30,
    ),
    limit: int = Query(
        20,
        ge=1,
        le=settings.max_news_items,
    ),
) -> NewsResponse:
    source_key = source.lower()
    scraper = SCRAPERS.get(source_key)

    if scraper is None:
        raise HTTPException(
            status_code=404,
            detail={
                "message": "Fonte não cadastrada",
                "available_sources": sorted(SCRAPERS),
            },
        )

    cache_key = (
        source_key,
        limit,
    )

    # ========================================================
    # 1. CACHE
    # ========================================================

    cached, is_fresh = get_cache(cache_key)

    if cached is not None and is_fresh:
        logger.info(
            "[NEWS] CACHE HIT: source=%s limit=%s",
            source_key,
            limit,
        )
        age = cache_age_seconds(cache_key) or 0
        response.headers["X-Cache"] = "HIT"
        response.headers["X-Cache-Age"] = str(age)
        response.headers["Cache-Control"] = "no-store"
        return cached

    # ========================================================
    # 2. CACHE STALE
    # ========================================================

    if cached is not None and not is_fresh:
        now = asyncio.get_running_loop().time()

        cooldown_end = blocked_until.get(
            cache_key,
            0.0,
        )

        if now < cooldown_end:
            remaining = int(cooldown_end - now) + 1

            logger.info(
                "[NEWS] Cache stale, mas fonte está em cooldown: "
                "source=%s restante=%ss",
                source_key,
                remaining,
            )
            age = cache_age_seconds(cache_key) or 0
            response.headers["X-Cache"] = "STALE"
            response.headers["X-Cache-Age"] = str(age)
            response.headers["Cache-Control"] = "no-store"
            return cached.model_copy(update={"stale": True, "age_seconds": age})

        logger.info(
            "[NEWS] CACHE STALE: source=%s limit=%s",
            source_key,
            limit,
        )

        start_background_refresh(
            cache_key=cache_key,
            scraper=scraper,
            limit=limit,
        )
        age = cache_age_seconds(cache_key) or 0
        response.headers["X-Cache"] = "STALE"
        response.headers["X-Cache-Age"] = str(age)
        response.headers["Cache-Control"] = "no-store"
        return cached.model_copy(update={"stale": True, "age_seconds": age})

    # ========================================================
    # 3. VERIFICA COOLDOWN
    # ========================================================

    now = asyncio.get_running_loop().time()

    cooldown_end = blocked_until.get(
        cache_key,
        0.0,
    )

    if now < cooldown_end:
        remaining = int(cooldown_end - now) + 1
        logger.warning(
            "[NEWS] Fonte em cooldown: " "source=%s restante=%ss",
            source_key,
            remaining,
        )

        # ----------------------------------------------------
        # Se tivermos cache, devolve ele.
        # ----------------------------------------------------

        if cached is not None:
            return cached.model_copy(update={"stale": True})

        # ----------------------------------------------------
        # Sem cache: informa usuário para tentar depois.
        # ----------------------------------------------------

        raise HTTPException(
            status_code=503,
            detail=(
                "A fonte está temporariamente indisponível. "
                "Uma nova tentativa está sendo processada "
                "em background. Tente novamente em até 1 minuto."
            ),
            headers={"Retry-After": str(min(remaining, 60))},
        )

    # ========================================================
    # 4. CACHE MISS
    # ========================================================

    logger.info(
        "[NEWS] CACHE MISS: source=%s limit=%s",
        source_key,
        limit,
    )

    # ========================================================
    # 5. SCRAPE INICIAL
    # ========================================================

    async with scrape_semaphore:

        # ----------------------------------------------------
        # Outra requisição pode ter preenchido cache
        # enquanto aguardávamos o semaphore.
        # ----------------------------------------------------

        cached, is_fresh = get_cache(cache_key)

        if cached is not None and is_fresh:

            logger.info("[NEWS] CACHE preenchido enquanto aguardava")
            age = cache_age_seconds(cache_key) or 0
            response.headers["X-Cache"] = "HIT"
            response.headers["X-Cache-Age"] = str(age)
            response.headers["Cache-Control"] = "no-store"
            return cached

        try:

            await browser_manager.start()

            if browser_manager.context is None:
                raise ScraperError("Contexto Playwright não foi criado")

            logger.info(
                "[NEWS] Fazendo scrape inicial: " "source=%s limit=%s",
                source_key,
                limit,
            )

            items = await scraper.scrape(
                browser_manager,
                limit,
            )

        except asyncio.CancelledError:

            raise

        except ScraperError as exc:
            logger.warning(
                "[NEWS] Scrape inicial falhou: source=%s erro=%s",
                source_key,
                exc,
            )

            if is_block_error(exc):
                blocked_until[cache_key] = (
                    asyncio.get_running_loop().time()
                    + settings.blocked_cooldown_seconds
                )

                start_background_refresh(
                    cache_key=cache_key,
                    scraper=scraper,
                    limit=limit,
                )

                cached, _ = get_cache(cache_key)
                if cached is not None:
                    age = cache_age_seconds(cache_key) or 0
                    response.headers["X-Cache"] = "STALE"
                    response.headers["X-Cache-Age"] = str(age)
                    response.headers["Cache-Control"] = "no-store"
                    return cached.model_copy(update={"stale": True, "age_seconds": age})

                raise HTTPException(
                    status_code=503,
                    detail=(
                        "A fonte recusou temporariamente a consulta. "
                        "Tente novamente em até 1 minuto."
                    ),
                    headers={"Retry-After": "60"},
                ) from exc

            cached, _ = get_cache(cache_key)
            if cached is not None:
                age = cache_age_seconds(cache_key) or 0
                response.headers["X-Cache"] = "STALE"
                response.headers["X-Cache-Age"] = str(age)
                response.headers["Cache-Control"] = "no-store"
                return cached.model_copy(update={"stale": True, "age_seconds": age})

            raise HTTPException(status_code=502, detail=str(exc)) from exc

        except Exception as exc:

            logger.exception(
                "[NEWS] Erro inesperado: source=%s",
                source_key,
            )

            cached, _ = get_cache(cache_key)

            if cached is not None:
                age = cache_age_seconds(cache_key) or 0
                response.headers["X-Cache"] = "STALE"
                response.headers["X-Cache-Age"] = str(age)
                response.headers["Cache-Control"] = "no-store"
                return cached.model_copy(update={"stale": True, "age_seconds": age})

            raise HTTPException(
                status_code=503,
                detail=str(exc),
            ) from exc

        # ========================================================
        # 6. VALIDA RESULTADO
        # ========================================================

        if items is None:

            raise HTTPException(
                status_code=503,
                detail=("O scraper não retornou nenhuma notícia."),
            )

        items = list(items)

        if not items:

            raise HTTPException(
                status_code=503,
                detail=("O scraper retornou uma lista vazia."),
            )

        # ========================================================
        # 7. MONTA RESPONSE
        # ========================================================

        result = NewsResponse(
            source=scraper.source_name,
            fetched_at=datetime.now(timezone.utc),
            count=len(items),
            stale=False,
            age_seconds=0,
            items=items,
        )

        # ========================================================
        # 8. SALVA CACHE
        # ========================================================

        cache[cache_key] = (
            asyncio.get_running_loop().time(),
            result,
        )

        blocked_until.pop(
            cache_key,
            None,
        )

        logger.info(
            "[NEWS] Cache atualizado: " "source=%s limit=%s items=%s",
            source_key,
            limit,
            len(items),
        )
        response.headers["X-Cache"] = "MISS"
        response.headers["X-Cache-Age"] = "0"
        response.headers["Cache-Control"] = "no-store"
        return result


# ============================================================
# FUNDAMENTUS
# ============================================================


@app.get("/tickers")
async def fundamentus(
    source: str = Query(
        "fundamentus",
        min_length=1,
        max_length=30,
    ),
    ticker: str = Query(
        "PETR4",
        min_length=1,
    ),
):
    source_key = source.lower()
    scraper = SCRAPERS.get(source_key)
    dados = await scraper.searchFundamentusHttpx(ticker.upper())
    # await browser_manager.start(useProxy=False)
    # if browser_manager.context is None:
    #     raise ScraperError("Contexto Playwright não foi criado")
    # dados = await scraper.search(browser_manager.context, ticker.upper())
    # async with browser_manager.scrape_session() as (page, context):
    #     dados = await scraper.search(context, ticker.upper())
    return dados


@app.get("/tickers/historical")
async def fundamentusHistorical(
    source: str = Query(
        "fundamentus",
        min_length=1,
        max_length=30,
        description="Fonte dos dados (fundamentus, yahoo, etc)",
    ),
    ticker: str = Query(
        "PETR4", min_length=1, description="Código do ativo (ex: PETR4, VALE3)"
    ),
    range: str = Query(
        "1mo",
        min_length=1,
        max_length=10,
        description="Intervalo dos dados (1d, 1wk, 1mo, etc)",
    ),
    interval: str = Query(
        "1mo",
        min_length=1,
        max_length=10,
        description="Intervalo dos dados (1d, 1wk, 1mo, etc)",
    ),
    order: str = Query(
        "asc",
        min_length=3,
        max_length=4,
        description="Ordem de classificação (asc ou desc)",
    ),
):
    if order.lower() not in ["asc", "desc"]:
        raise HTTPException(
            status_code=400, detail="O parâmetro 'order' deve ser 'asc' ou 'desc'"
        )
    source_key = source.lower()
    scraper = SCRAPERS.get(source_key)
    order_map = {"asc": False, "desc": True}
    is_descending = order_map.get(order.lower(), False)

    # await browser_manager.start()
    # if browser_manager.context is None:
    #     raise ScraperError("Contexto Playwright não foi criado")
    # dados = await scraper.scrape(
    #     browser_manager.context, ticker.upper(), range, is_descending
    # )
    dados = await scraper.historicalFundamentusHttpx(
        ticker.upper(), range, is_descending
    )
    # async with browser_manager.scrape_session() as (page, context):
    #     dados = await scraper.scrape(context, ticker.upper(), range, is_descending)
    return dados


# ============================================================
# SOURCES
# ============================================================


@app.get("/sources")
async def sources() -> dict[str, list[str]]:
    return {
        "sources": sorted(SCRAPERS),
    }
