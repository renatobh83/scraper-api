from app.scrapers.base import QuoteScraper, ScraperError
from fastapi import HTTPException

import httpx
from bs4 import BeautifulSoup


class Investidor10Scraper(QuoteScraper):
    source_name = "investidor10"
    urlBusca = "https://investidor10.com.br/busca/?s="
    baseUrl = "https://investidor10.com.br/fiis/"
    
    async def scrape(self, ticker:str):
        if not ticker:
            raise HTTPException(
                status_code=503,
                detail=("O Ticker deve ser informado."),
            )
        busca = f"{self.urlBusca}{ticker}/"
        try:
            with httpx.Client(
                verify=False,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",

                    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
                },
                timeout=10.0,
            ) as client:

                r = client.get(busca)
                r.raise_for_status()

                soup = BeautifulSoup(r.text, "html.parser")

                search = soup.select_one("div.search-content")
                
                print(search)
        except:
            raise
