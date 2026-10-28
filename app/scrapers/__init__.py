from app.scrapers.base import NewsScraper
from app.scrapers.bloomberg import BloombergHeadlinesScraper
from app.scrapers.broadcast import BroadcastHeadlinesScraper
from app.scrapers.fundamentus import FundamentusHeadlinesScraper
from app.scrapers.investidor10 import Investidor10Scraper
from app.scrapers.investing import InvestingHeadlinesScraper

# SCRAPERS: dict[str, NewsScraper] = {
SCRAPERS = {
    "investing": InvestingHeadlinesScraper(),
    "fundamentus": FundamentusHeadlinesScraper(),
    "broadcast": BroadcastHeadlinesScraper(),
    "investidor10": Investidor10Scraper(),
}

__all__ = ["NewsScraper", "SCRAPERS"]
