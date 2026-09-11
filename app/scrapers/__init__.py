from app.scrapers.base import NewsScraper
from app.scrapers.fundamentus import FundamentusHeadlinesScraper
from app.scrapers.investing import InvestingHeadlinesScraper
from app.scrapers.statusInvest import StatusInvestHeadlinesScraper

SCRAPERS: dict[str, NewsScraper] = {
    "investing": InvestingHeadlinesScraper(),
    "fundamentus": FundamentusHeadlinesScraper(),
    "statusinvest": StatusInvestHeadlinesScraper(),
}

__all__ = ["NewsScraper", "SCRAPERS"]
