from app.scrapers.base import NewsScraper
from app.scrapers.fundamentus import FundamentusHeadlinesScraper
from app.scrapers.investing import InvestingHeadlinesScraper

SCRAPERS: dict[str, NewsScraper] = {
    "investing": InvestingHeadlinesScraper(),
    "fundamentus": FundamentusHeadlinesScraper(),
}

__all__ = ["NewsScraper", "SCRAPERS"]
