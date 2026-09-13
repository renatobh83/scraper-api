from app.scrapers.base import MlScraper, ScraperError
from playwright_stealth import Stealth
class MercadoLivreScraper(MlScraper):
    
    
    
    async def scrape(
        self,
        context: BrowserContext,
        link: str
    ):
        page = await context.new_page()
        print("Abrindo a pagina")
        urlProduto = "https://www.mercadolivre.com.br/bicicleta-ergometrica-spinning-winnek-sync-magnetica-com-roda-de-inercia-13kg-e-bluetooth-para-app-fitshow/p/MLB65112472"
        try:
            stealth = Stealth()
            await stealth.apply_stealth_async(page)
            # page.set_default_timeout(settings.selector_timeout_ms)
            # page.set_default_navigation_timeout(settings.navigation_timeout_ms)  
            response = await page.goto(
                    urlProduto,
                    wait_until="commit",
                    timeout=15000,
                )
            print("STATUS:", response.status if response else None)
            print("URL:", page.url)
            print("TITLE:", await page.title())
            try:
                await page.wait_for_selector(
                    '.ui-pdp-title',
                    timeout = 15000
                )
            except:
                print("erro")
            html = await page.content()
            print(html)
        finally: 
            await page.close() 
            await context.close()