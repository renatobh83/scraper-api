import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query

from app.browser import BrowserManager
from app.config import settings
from app.models import HealthResponse, NewsResponse
from app.scrapers import SCRAPERS
from app.scrapers.base import ScraperError


browser_manager = BrowserManager()
scrape_semaphore = asyncio.Semaphore(settings.max_concurrent_scrapes)
cache: dict[tuple[str, int], tuple[float, NewsResponse]] = {}
blocked_until: dict[tuple[str, int], float] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # O Chromium é iniciado de forma preguiçosa na primeira chamada a /news.
    yield
    await browser_manager.close()


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="API inicial para coleta de notícias com Playwright.",
    lifespan=lifespan,
)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", version=settings.app_version)


@app.get("/news", response_model=NewsResponse)
async def news(
    source: str = Query("investing", min_length=1, max_length=30),
    limit: int = Query(20, ge=1, le=settings.max_news_items),
    refresh: bool = Query(
        False,
        description="Ignora o cache e força uma nova navegação na fonte.",
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

    cache_key = (source_key, limit)
    now = asyncio.get_running_loop().time()
    cached = cache.get(cache_key)
    if (
        cached
        and not refresh
        and now - cached[0] < settings.cache_ttl_seconds
    ):
        return cached[1]

    cooldown_end = blocked_until.get(cache_key, 0.0)
    if now < cooldown_end:
        if cached:
            return cached[1].model_copy(update={"stale": True})
        raise HTTPException(
            status_code=429,
            detail="A fonte bloqueou temporariamente novas consultas; tente novamente depois.",
            headers={"Retry-After": str(int(cooldown_end - now) + 1)},
        )

    # Uma única navegação externa por vez reduz CPU, memória e pressão na fonte.
    async with scrape_semaphore:
        now = asyncio.get_running_loop().time()
        cached = cache.get(cache_key)
        if (
            cached
            and not refresh
            and now - cached[0] < settings.cache_ttl_seconds
        ):
            return cached[1]

        try:
            await browser_manager.start()
            if browser_manager.context is None:
                raise ScraperError("Contexto Playwright não foi criado")

            items = await scraper.scrape(browser_manager.context, limit)
        except ScraperError as exc:
            if "HTTP 403" in str(exc):
                blocked_until[cache_key] = (
                    asyncio.get_running_loop().time()
                    + settings.blocked_cooldown_seconds
                )

            # Se a fonte bloquear uma nova consulta, preserve o último resultado
            # válido em vez de transformar um bloqueio transitório em falha total.
            if cached:
                return cached[1].model_copy(update={"stale": True})
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except Exception as exc:
            if cached:
                return cached[1].model_copy(update={"stale": True})
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        result = NewsResponse(
            source=scraper.source_name,
            fetched_at=datetime.now(timezone.utc),
            count=len(items),
            items=list(items),
        )
        blocked_until.pop(cache_key, None)
        cache[cache_key] = (asyncio.get_running_loop().time(), result)
        return result


@app.get("/sources")
async def sources() -> dict[str, list[str]]:
    return {"sources": sorted(SCRAPERS)}
