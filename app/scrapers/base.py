from abc import ABC, abstractmethod
from collections.abc import Sequence

from playwright.async_api import BrowserContext

from app.browser import BrowserManager
from app.utils.models import NewsItem


class ScraperError(RuntimeError):
    """Erro esperado ao consultar ou interpretar uma fonte de notícias."""


class NewsScraper(ABC):
    source_name: str

    @abstractmethod
    async def scrape(
        self,
        context: BrowserContext | BrowserManager,
        limit: int,
    ) -> Sequence[NewsItem]:
        raise NotImplementedError


class MlScraper(ABC):
    source_name: str

    @abstractmethod
    async def scrape(
        self,
        context: BrowserContext,
        limit: int,
    ) -> Sequence[NewsItem]:
        raise NotImplementedError

class QuoteScraper(ABC):
    source_name: str

    @abstractmethod
    async def scrape(
        self,
        ticker: str,
    ) :
        raise NotImplementedError
