import asyncio
import os
import logging
from urllib.parse import unquote, urlsplit, urlunsplit
from playwright.async_api import BrowserContext, async_playwright
from typing import Any
from app.config import settings

logger = logging.getLogger("uvicorn.error")


class BrowserManager:
    """Mantém uma única instância do Chrome (perfil persistente) por processo da API."""

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
        self._contexts: dict[str, BrowserContext] = {}
        self._spare_pages: dict[str, Any] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self.debug = os.getenv("DEBUG_BROWSER", "0") == "1"
        self.proxy = self._load_proxy()
        self.block_google = os.getenv("BLOCK_GOOGLE", "1") == "1"
        self.block_resources = os.getenv("BLOCK_RESOURCES", "1") == "1"
        self.headless = os.getenv("BROWSER_HEADLESS", "1") == "1"
        self.record_har = os.getenv("RECORD_HAR", "0") == "1"  # só para debug
        self._spare_page = None
        # opcional: se vazio, usa o UA real do Chrome instalado (mais coerente)
        self.user_agent = os.getenv("BROWSER_USER_AGENT") or None
    
    def _context_key(self, use_proxy: bool, profile: str) -> str:
        return f"{profile}:{'proxy' if use_proxy else 'noproxy'}"

    async def _get_lock(self, key: str) -> asyncio.Lock:
        async with self._global_lock:
            if key not in self._locks:
                self._locks[key] = asyncio.Lock()
            return self._locks[key]

    
    async def start( self, data_dir: str, useProxy: bool = True) -> BrowserContext:
        key = self._context_key(useProxy, data_dir)

        if key in self._contexts:
            return key 
            
        lock = await self._get_lock(key)

        async with lock:
            if key in self._contexts:
                return self._contexts[key]

            if self._playwright is None:
                self._playwright = await async_playwright().start()

            kwargs = dict(
                user_data_dir=f"perfil_{data_dir}_{'proxy' if useProxy else 'noproxy'}",
                headless=self.headless,
                slow_mo=150 if self.debug else 0,
                proxy=self.proxy if useProxy and self.proxy else None,
                locale="pt-BR",
                viewport={"width": 1280, "height": 900},
                accept_downloads=False,
                ignore_default_args=["--enable-automation"],
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",           # você já tem
                    "--no-first-run",
                    "--no-default-browser-check",

                    # --- economia de RAM ---
                    # "--disable-extensions",
                    # "--disable-component-extensions-with-background-pages",
                    # "--disable-background-networking",
                    # "--disable-background-timer-throttling",
                    # "--disable-backgrounding-occluded-windows",
                    # "--disable-renderer-backgrounding",
                    # "--disable-features=Translate,BackForwardCache,AcceptCHFrame,MediaRouter,OptimizationHints,CalculateNativeWinOcclusion",
                    # "--disable-ipc-flooding-protection",
                    # "--disable-hang-monitor",
                    # "--disable-prompt-on-repost",
                    # "--disable-sync",
                    # "--metrics-recording-only",
                    # "--no-service-autorun",
                    # "--password-store=basic",
                    # "--use-mock-keychain",
                    # "--js-flags=--max-old-space-size=256",   # limita heap V8 por renderer
                    # "--renderer-process-limit=1",            # MUITO importante: 1 renderer só
                    # "--disable-gpu",
                    # "--disable-software-rasterizer",
                    # "--disable-gpu-compositing",
                    # "--memory-pressure-off",
                ],
            )
            # kwargs["service_workers"] = "block" if not useProxy else "allow"
            if self.user_agent:
                kwargs["user_agent"] = self.user_agent
            
            if self.record_har:
                kwargs.update(
                    record_har_path=f"debug_{key.replace(':', '_')}.har",
                    record_har_content="embed",
                )
            context = await self._playwright.chromium.launch_persistent_context(**kwargs)
            # depois de criar o contexto, ANTES de qualquer page.goto
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'languages', { get: () => ['pt-BR', 'pt', 'en'] });
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                window.chrome = { runtime: {} };
            """)
            if self.block_resources or self.block_google:
                await context.route("**/*", self._handle_route)
            self._contexts[key] = context
            self._spare_pages[key] = context.pages[0] if context.pages else None
            
            return key

        # if self.context is not None:
        #     try:
        #         await self.context.close()
        #     except Exception:
        #         pass
        #     self.context = None
        #     self.browser = None
        #     self.playwright = None

        # async with self._start_lock:
        #     if self.context is not None:
        #         return

        #     self._playwright = await async_playwright().start()

        #     kwargs = dict(
        #         user_data_dir=f"perfil_{data_dir}",
        #       #  channel="chrome",
        #         headless=self.headless,
        #         slow_mo=150 if self.debug else 0,
        #         proxy=self.proxy if useProxy and self.proxy else None,
        #         locale="pt-BR",
        #         viewport={"width": 1280, "height": 900},
        #         accept_downloads=False,
        #         service_workers="block",
        #         ignore_default_args=["--enable-automation"],
        #         args=[
        #             "--disable-blink-features=AutomationControlled",
        #             "--disable-dev-shm-usage",
        #             "--no-first-run",
        #             "--no-default-browser-check",
        #         ],
        #     )
        #     if self.user_agent:
        #         kwargs["user_agent"] = self.user_agent
        #     if self.record_har:
        #         kwargs.update(record_har_path="debug.har", record_har_content="embed")

        #     try:
        #         self.context = (
        #             await self._playwright.chromium.launch_persistent_context(**kwargs)
        #         )
        #         if self.block_resources or self.block_google:
        #             await self.context.route("**/*", self._handle_route)
        #         self._spare_page = self.context.pages[0] if self.context.pages else None
        #     except Exception:
        #         # não deixa o Playwright pendurado se o launch falhar
        #         await self.close()
        #         raise

    async def _handle_route(self, route) -> None:
        hostname = urlsplit(route.request.url).hostname or ""

        # nunca bloquear o Cloudflare, senão o desafio não completa
        if hostname.endswith("cloudflare.com"):
            await route.continue_()
            return

        if self.block_google and self._is_blocked_google_host(hostname):
            await route.abort(error_code="blockedbyclient")
            return

        if (
            self.block_resources
            and route.request.resource_type in self.BLOCKED_RESOURCE_TYPES
        ):
            await route.abort(error_code="blockedbyclient")
            return

        await route.continue_()

    async def close(self) -> None:
        """Compat: delega para close_all()."""
        await self.close_all()

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

            netloc = f"{parsed.hostname}:{parsed.port}"
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

    @classmethod
    def _is_blocked_google_host(cls, hostname: str | None) -> bool:
        if not hostname:
            return False

        hostname = hostname.lower().rstrip(".")

        return any(
            hostname == domain or hostname.endswith(f".{domain}")
            for domain in cls.BLOCKED_GOOGLE_DOMAINS
        )
    def has_context(self, key: str) -> bool:
        ctx = self._contexts.get(key)
        return ctx is not None



    async def new_page(self, key):
        ctx = self._contexts.get(key)
        
        if ctx is None:
            raise RuntimeError(f"Contexto '{key}' não iniciado")

        page = None
        spare = self._spare_pages.get(key)
        
        if spare is not None and not spare.is_closed():
            page = spare
        self._spare_pages[key] = None
        if page is None:
            page = await ctx.new_page()

        page.set_default_timeout(settings.selector_timeout_ms)
        page.set_default_navigation_timeout(settings.navigation_timeout_ms)
        return page

    async def close_all(self) -> None:
    
        for key, ctx in list(self._contexts.items()):
            try:
                await ctx.close()
            except Exception as e:
                logger.warning(f"[Browser] Erro fechando {key}: {e}")
        self._contexts.clear()
        self._spare_pages.clear()
        self._locks.clear()                    # ⬅️
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception as e:
                logger.warning(f"[Browser] Erro parando Playwright: {e}")
            finally:
                self._playwright = None

    async def close_context(self, key: str) -> None:
        """Fecha um contexto específico, liberando conexões TCP com o proxy."""
        ctx = self._contexts.pop(key, None)
        self._spare_pages.pop(key, None)

        if ctx is None:
            return

        try:
            await ctx.close()
            logger.info(f"[Browser] Contexto fechado: {key}")
        except Exception as e:
            logger.warning(f"[Browser] Erro fechando contexto {key}: {e}")