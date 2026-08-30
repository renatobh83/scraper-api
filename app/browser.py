import asyncio
import os
from contextlib import asynccontextmanager
from urllib.parse import unquote, urlsplit, urlunsplit
from playwright.async_api import Browser, BrowserContext, Page, async_playwright


from app.config import settings


class BrowserManager:
    """Mantém uma única instância do Chromium por processo da API."""

    BLOCKED_RESOURCE_TYPES = {"image", "media", "font", "stylesheet"}
    BLOCKED_GOOGLE_DOMAINS = {
        "google.com",
        "google.com.br",
        "googleapis.com",
        "gstatic.com",
        "googleusercontent.com",
        "google-analytics.com",
        "doubleclick.net",
        "googlesyndication.com",
        "monetization.prod.invmed",
    }

    def __init__(self) -> None:
        self._playwright = None
        self._playwright_lock = asyncio.Lock()
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self._start_lock = asyncio.Lock()
        self.debug = os.getenv("DEBUG_BROWSER", "0") == "1"
        self.debug_pause = os.getenv("DEBUG_PAUSE", "0") == "1"
        self.proxy = self._load_proxy()
        self.block_google = os.getenv("BLOCK_GOOGLE", "1") == "1"

    async def _ensure_playwright(self) -> None:
        if self._playwright is not None:
            return
        async with self._playwright_lock:
            if self._playwright is None:
                self._playwright = await async_playwright().start()

    async def start(self) -> None:
        # O navegador só é criado na primeira chamada a /news.
        if self.browser is not None:
            return

        async with self._start_lock:
            if self.browser is not None:
                return

            self._playwright = await async_playwright().start()
            self.browser = await self._playwright.chromium.launch(
                headless=not self.debug,
                slow_mo=150 if self.debug else 0,
                proxy=self.proxy,
                args=[
                    "--disable-dev-shm-usage",
                    "--disable-extensions",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-background-networking",
                    "--disable-background-timer-throttling",
                    "--disable-backgrounding-occluded-windows",
                    "--disable-renderer-backgrounding",
                    "--disable-ipc-flooding-protection",
                ],
            )

            self.context = await self.browser.new_context(
                locale="pt-BR",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/121.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 900},
                accept_downloads=False,
                java_script_enabled=True,
                service_workers="block",
            )
            await self.context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });
                """)
            await self.context.route("**/*", self._handle_route)

    @asynccontextmanager
    async def scrape_session(self):
        """
        Abre um browser + context + page isolados para UMA operação de scrape
        e fecha tudo ao sair do bloco. Cada chamada usa seu próprio browser
        (garante IP/túnel novo no proxy) sem interferir em scrapes concorrentes.

        Uso:
            async with browser_manager.scrape_session() as (page, context):
                ...
        """
        await self._ensure_playwright()

        browser: Browser = await self._playwright.chromium.launch(
            headless=not self.debug,
            slow_mo=150 if self.debug else 0,
            proxy=self.proxy,
            args=[
                "--disable-dev-shm-usage",
                "--disable-extensions",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-background-networking",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                "--disable-ipc-flooding-protection",
            ],
        )
        try:
            context: BrowserContext = await browser.new_context(
                locale="pt-BR",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/121.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 900},
                accept_downloads=False,
                java_script_enabled=True,
                service_workers="block",
            )
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });
                """)
            await context.route("**/*", self._handle_route)

            page: Page = await context.new_page()
            page.set_default_timeout(settings.selector_timeout_ms)
            page.set_default_navigation_timeout(settings.navigation_timeout_ms)

            try:
                yield page, context
            finally:
                await context.close()
        finally:
            await browser.close()

    @staticmethod
    def _load_proxy() -> dict[str, str] | None:
        """Carrega proxy sem registrar a URL nem as credenciais."""
        proxy_url = os.getenv("PROXY_URL")
        proxy_server = os.getenv("PROXY_SERVER")
        proxy_username = os.getenv("PROXY_USERNAME")
        proxy_password = os.getenv("PROXY_PASSWORD")

        if proxy_url:
            parsed = urlsplit(proxy_url)
            if not parsed.scheme or not parsed.hostname or not parsed.port:
                raise ValueError(
                    "PROXY_URL deve ter o formato http://usuario:senha@host:porta"
                )

            netloc = parsed.hostname
            if parsed.port:
                netloc = f"{netloc}:{parsed.port}"
            proxy = {
                "server": urlunsplit(
                    (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
                )
            }
            if parsed.username:
                proxy["username"] = unquote(parsed.username)
            if parsed.password:
                proxy["password"] = unquote(parsed.password)
            return proxy

        if proxy_server:
            proxy = {"server": proxy_server}
            if proxy_username:
                proxy["username"] = proxy_username
            if proxy_password:
                proxy["password"] = proxy_password
            return proxy

        return None

    async def close(self) -> None:
        """Encerra o driver do Playwright (chamar no shutdown da app)."""
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
        # if self.context is not None:
        #     await self.context.close()
        #     self.context = None

        # if self.browser is not None:
        #     await self.browser.close()
        #     self.browser = None

        # if self._playwright is not None:
        #     await self._playwright.stop()
        #     self._playwright = None

    async def _handle_route(self, route) -> None:
        request_url = route.request.url
        hostname = urlsplit(request_url).hostname

        if self.block_google and self._is_blocked_google_host(hostname):

            await route.abort(error_code="blockedbyclient")
            return

        if route.request.resource_type in self.BLOCKED_RESOURCE_TYPES:
            await route.abort(error_code="blockedbyclient")
            return

        await route.continue_()

    @classmethod
    def _is_blocked_google_host(cls, hostname: str | None) -> bool:
        if not hostname:
            return False

        hostname = hostname.lower().rstrip(".")

        return any(
            hostname == domain or hostname.endswith(f".{domain}")
            for domain in cls.BLOCKED_GOOGLE_DOMAINS
        )

    async def new_page(self):
        await self.restart()

        page = await self.context.new_page()
        page.set_default_timeout(settings.selector_timeout_ms)
        page.set_default_navigation_timeout(settings.navigation_timeout_ms)
        return page

    async def restart(self) -> None:
        await self.close()  # já existe: fecha context, browser e playwright
        await self.start()  # relança tudo do zero
