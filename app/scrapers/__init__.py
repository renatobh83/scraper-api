from app.scrapers.base import NewsScraper
from app.scrapers.bloomberg import BloombergHeadlinesScraper
from app.scrapers.broadcast import BroadcastHeadlinesScraper
from app.scrapers.fundamentus import FundamentusHeadlinesScraper
from app.scrapers.investing import InvestingHeadlinesScraper
from app.scrapers.investidor10 import Investidor10Scraper

SCRAPERS: dict[str, NewsScraper] = {
    "investing": InvestingHeadlinesScraper(),
    "fundamentus": FundamentusHeadlinesScraper(),
    "broadcast": BroadcastHeadlinesScraper(),
    "teste": Investidor10Scraper()
}

__all__ = ["NewsScraper", "SCRAPERS"]
