from dataclasses import dataclass
import os

from dotenv import load_dotenv

# Load the .env file
load_dotenv()


@dataclass(frozen=True)
class Settings:
    api_key: str = os.getenv("API_KEY")
    app_name: str = os.getenv("APP_NAME", "News Scraper API")
    app_version: str = os.getenv("APP_VERSION", "0.2.0")
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = int(os.getenv("PORT", "8000"))
    navigation_timeout_ms: int = int(os.getenv("NAVIGATION_TIMEOUT_MS", "60000"))
    selector_timeout_ms: int = int(os.getenv("SELECTOR_TIMEOUT_MS", "10000"))
    cache_ttl_seconds: int = int(os.getenv("CACHE_TTL_SECONDS", "120"))
    blocked_cooldown_seconds: int = int(os.getenv("BLOCKED_COOLDOWN_SECONDS", "60"))
    max_news_items: int = int(os.getenv("MAX_NEWS_ITEMS", "20"))
    max_concurrent_scrapes: int = int(os.getenv("MAX_CONCURRENT_SCRAPES", "3"))
    news_cache_limit: int = 50


settings = Settings()
