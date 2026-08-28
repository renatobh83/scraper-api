import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query

from app.browser import BrowserManager
from app.config import settings
from app.models import HealthResponse, NewsResponse
from app.scrapers import SCRAPERS
from app.scrapers.base import ScraperError
from app.scrapers.cache.news_cache import (
    news_cache_scheduler,
)

scrape_semaphore = asyncio.Semaphore(settings.max_concurrent_scrapes)
cache: dict[tuple[str, int], tuple[float, NewsResponse]] = {}
cache_refresh_tasks: dict[tuple, asyncio.Task] = {}
blocked_until: dict[tuple[str, int], float] = {}
browser_manager = BrowserManager()


news_scheduler_task = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global news_scheduler_task

    news_scheduler_task = asyncio.create_task(
        news_cache_scheduler(
            scrapers=SCRAPERS,
            browser_manager=browser_manager,
            cache=cache,
            scrape_semaphore=scrape_semaphore,
            cache_limit=settings.news_cache_limit,
            interval=settings.cache_ttl_seconds,
        )
    )

    try:
        yield
    finally:
        if news_scheduler_task is not None:
            news_scheduler_task.cancel()
            try:
                await news_scheduler_task
            except asyncio.CancelledError:
                pass

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

    cached = cache.get(source_key)

    if cached is None:
        raise HTTPException(
            status_code=503,
            detail="Cache ainda não foi carregado.",
        )

    result = cached[1]

    return result.model_copy(
        update={
            "items": result.items[:limit],
            "count": min(limit, len(result.items)),
        }
    )


@app.get("/sources")
async def sources() -> dict[str, list[str]]:
    return {"sources": sorted(SCRAPERS)}
