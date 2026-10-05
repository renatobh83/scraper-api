from datetime import datetime

from pydantic import BaseModel, Field, HttpUrl


class NewsItem(BaseModel):
    title: str
    url: HttpUrl
    source: str
    published_at: datetime | None = None
    summary: str | None = None
    symbols: list[str] = Field(default_factory=list)


class NewsResponse(BaseModel):
    source: str
    fetched_at: datetime
    count: int
    stale: bool = False
    age_seconds: int | None = None
    items: list[NewsItem] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: str
    version: str
