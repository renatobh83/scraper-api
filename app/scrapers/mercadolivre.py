from app.scrapers.base import MlScraper, ScraperError
from playwright_stealth import Stealth
from playwright.async_api import async_playwright
import json
class MercadoLivreScraper(MlScraper):
    
    
    
    async def scrape(
        self,
        context: BrowserContext,
        link: str
    ):
        
           
        data = json.load(open("ml_auth.json"))
        context.add_cookies(data["cookies"])
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
                    wait_until="domcontentloaded",
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
            except Exception as e:
                print(f"⚠️ Aviso: Seletor '.ui-pdp-title' não encontrado. Página pode ser um CAPTCHA ou erro. {e}")

            # ==========================================
            # EXTRAÇÃO DOS DADOS (O SEU CÓDIGO JS)
            # ==========================================
            dados_extraidos = await page.evaluate("""
                () => {
                    const extractPrice = (el) => {
                        if (!el) return null;
                        const integer = el.querySelector(".andes-money-amount__fraction")?.textContent.trim() || "0";
                        const cents = el.querySelector(".andes-money-amount__cents")?.textContent.trim() || "00";
                        return parseFloat(`${integer}.${cents}`);
                    };

                    const metaPrice = document.querySelector('meta[itemprop="price"]');
                    let currentPrice = metaPrice ? parseFloat(metaPrice.getAttribute('content')) : null;

                    const originalPriceEl = document.querySelector('.ui-pdp-price__original-value');
                    let originalPrice = extractPrice(originalPriceEl);

                    if (currentPrice === null) {
                        const currentPriceEl = document.querySelector('.ui-pdp-price__second-line .ui-pdp-price__part');
                        currentPrice = extractPrice(currentPriceEl);
                    }

                    if (originalPrice === null) {
                        const allPrices = document.querySelectorAll('.ui-pdp-price__part');
                        if (allPrices.length >= 2) {
                            originalPrice = extractPrice(allPrices[0]);
                            if (currentPrice === null) currentPrice = extractPrice(allPrices[1]);
                        } else if (allPrices.length === 1 && currentPrice === null) {
                            currentPrice = extractPrice(allPrices[0]);
                        }
                    }

                    const img = document.querySelector(".ui-pdp-gallery__figure__image");
                    
                    const listItems = document.querySelectorAll(".ui-vpp-highlighted-specs__features-list li");
                    let description = null;
                    if (listItems.length > 0) {
                        description = Array.from(listItems).map((li) => li.textContent.trim()).join(" | ");
                    } else {
                        const content = document.querySelector('[data-testid="content"]');
                        description = content ? content.textContent.trim() : null;
                    }

                    return {
                        title: document.querySelector(".ui-pdp-title")?.textContent?.trim() || null,
                        prices: { original: originalPrice, current: currentPrice },
                        imageUrls: { 
                            src: img?.getAttribute("src") || null, 
                            zoom: img?.getAttribute("data-zoom") || null 
                        },
                        description: description,
                        extractedAt: new Date().toISOString(),
                        url: window.location.href
                    };
                }
            """)
            
            print(f"✅ Dados extraídos: {dados_extraidos.get('title')}")
            print(f"💰 Preço: R$ {dados_extraidos['prices'].get('current')}")
        finally: 
            
            await page.close() 
            # await context.close()