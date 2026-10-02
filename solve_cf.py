import asyncio
from dotenv import load_dotenv
from app.browser import BrowserManager

load_dotenv(override=True)


async def main():
    bm = BrowserManager()
    bm.headless = True
    bm.block_resources = False  # desligue os bloqueios no teste
    bm.block_google = False
    await bm.start()

    page = await bm.new_page()
    page.set_default_navigation_timeout(90000)
    await page.goto(
        "https://br.investing.com/news/headlines", wait_until="domcontentloaded"
    )

    try:
        await page.wait_for_function(
            "document.title && !/um momento|just a moment/i.test(document.title)",
            timeout=30000,
        )
    except Exception:
        pass

    print("título:", await page.title())
    print("url:", page.url)
    print("UA:", await page.evaluate("navigator.userAgent"))
    cookies = await bm.context.cookies("https://br.investing.com")
    print("cf_clearance:", any(c["name"] == "cf_clearance" for c in cookies))
    await page.screenshot(path="headless.png")

    await bm.close()
    await asyncio.sleep(0.5)


asyncio.run(main())
