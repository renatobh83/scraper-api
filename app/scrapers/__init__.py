from app.scrapers.base import NewsScraper
from app.scrapers.bloomberg import BloombergHeadlinesScraper
from app.scrapers.fundamentus import FundamentusHeadlinesScraper
from app.scrapers.investing import InvestingHeadlinesScraper

SCRAPERS: dict[str, NewsScraper] = {
    "investing": InvestingHeadlinesScraper(),
    "fundamentus": FundamentusHeadlinesScraper(),
    "teste": BloombergHeadlinesScraper(),
}

__all__ = ["NewsScraper", "SCRAPERS"]
