import asyncio
from dotenv import load_dotenv
from app.browser import BrowserManager

load_dotenv(override=True)


async def main():
    bm = BrowserManager()
    bm.headless = True
    bm.block_resources = False  # desligue os bloqueios no teste
    bm.block_google = False
    await bm.start("./perfil_boomerang", useProxy=False)

    page = await bm.new_page()
    page.set_default_navigation_timeout(90000)
    await page.goto(
        "https://www.bloomberg.com/latest?utm_source=homepage&utm_medium=web&utm_campaign=latest",
        wait_until="commit",
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
    await page.pause()
    await bm.close()
    await asyncio.sleep(0.5)


asyncio.run(main())
