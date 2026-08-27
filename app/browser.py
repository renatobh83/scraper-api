import asyncio
import os
from urllib.parse import unquote, urlsplit, urlunsplit

from playwright.async_api import Browser, BrowserContext, async_playwright

from app.config import settings
from playwright_stealth import Stealth  # Algumas versões usam assim

# No seu código:
stealth = Stealth()

class BrowserManager:
    """Mantém uma única instância do Chromium por processo da API."""

    BLOCKED_RESOURCE_TYPES = {"image", "media", "font"}

    def __init__(self) -> None:
        self._playwright = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self._start_lock = asyncio.Lock()
        self.debug = os.getenv("DEBUG_BROWSER", "0") == "1"
        self.debug_pause = os.getenv("DEBUG_PAUSE", "0") == "1"
        self.proxy = self._load_proxy()

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
                proxy=self.proxy,
                slow_mo=150 if self.debug else 0,
                args=[
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                    "--no-default-browser-check",
                ],
            
            )

            self.context = await self.browser.new_context(
                locale="pt-BR",
                user_agent=(
                     "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/151.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 900},
                accept_downloads=False,
                java_script_enabled=True,
                service_workers="block",extra_http_headers={
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
                }
            )
            await self.context.route("**/*", self._handle_route)

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
        if self.context is not None:
            await self.context.close()
            self.context = None

        if self.browser is not None:
            await self.browser.close()
            self.browser = None

        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def _handle_route(self, route) -> None:
        if route.request.resource_type in self.BLOCKED_RESOURCE_TYPES:
            await route.abort(error_code="blockedbyclient")
            return

        await route.continue_()

    async def new_page(self):
        if self.context is None:
            raise RuntimeError("BrowserManager não foi iniciado")

        page = await self.context.new_page()
        await stealth_async(page)
        page.set_default_timeout(settings.selector_timeout_ms)
        page.set_default_navigation_timeout(settings.navigation_timeout_ms)
        return page
