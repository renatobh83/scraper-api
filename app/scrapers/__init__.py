from app.scrapers.base import NewsScraper
from app.scrapers.investing import InvestingHeadlinesScraper


SCRAPERS: dict[str, NewsScraper] = {
    "investing": InvestingHeadlinesScraper(),
}

__all__ = ["NewsScraper", "SCRAPERS"]
