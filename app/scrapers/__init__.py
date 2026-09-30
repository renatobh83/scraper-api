from app.scrapers.base import NewsScraper
from app.scrapers.fundamentus import FundamentusHeadlinesScraper
from app.scrapers.investing import InvestingHeadlinesScraper
from app.scrapers.statusInvest import StatusInvestHeadlinesScraper
from app.scrapers.mercadolivre import MercadoLivreScraper

SCRAPERS: dict[str, NewsScraper] = {
    "investing": InvestingHeadlinesScraper(),
    "fundamentus": FundamentusHeadlinesScraper(),
    "statusinvest": StatusInvestHeadlinesScraper(),
    "mercadolivre": MercadoLivreScraper()
}

__all__ = ["NewsScraper", "SCRAPERS"]
