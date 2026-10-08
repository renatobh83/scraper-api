from app.scrapers.base import NewsScraper
from app.scrapers.bloomberg import BloombergHeadlinesScraper
from app.scrapers.broadcast import BroadcastHeadlinesScraper
from app.scrapers.fundamentus import FundamentusHeadlinesScraper
from app.scrapers.investing import InvestingHeadlinesScraper

SCRAPERS: dict[str, NewsScraper] = {
    "investing": InvestingHeadlinesScraper(),
    "fundamentus": FundamentusHeadlinesScraper(),
    "broadcast": BroadcastHeadlinesScraper(),
}

__all__ = ["NewsScraper", "SCRAPERS"]
