import asyncio
import os
import logging
from urllib.parse import unquote, urlsplit, urlunsplit
from playwright.async_api import BrowserContext, async_playwright

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
        self.context: BrowserContext | None = None
        self._start_lock = asyncio.Lock()
        self.debug = os.getenv("DEBUG_BROWSER", "0") == "1"
        self.proxy = self._load_proxy()
        self.block_google = os.getenv("BLOCK_GOOGLE", "1") == "1"
        self.block_resources = os.getenv("BLOCK_RESOURCES", "1") == "1"
        self.headless = os.getenv("BROWSER_HEADLESS", "1") == "1"
        self.record_har = os.getenv("RECORD_HAR", "0") == "1"  # só para debug
        self.profile_dir = os.getenv("PROFILE_DIR", "./perfil_investing")
        self._spare_page = None
        # opcional: se vazio, usa o UA real do Chrome instalado (mais coerente)
        self.user_agent = os.getenv("BROWSER_USER_AGENT") or None

    async def start(
        self, data_dir: str = "./perfil_investing", useProxy: bool = True
    ) -> None:

        if self.context is not None:
            try:
                await self.context.close()
            except Exception:
                pass
            self.context = None
            self.browser = None
            self.playwright = None

        async with self._start_lock:
            if self.context is not None:
                return

            self._playwright = await async_playwright().start()

            kwargs = dict(
                user_data_dir=f"perfil_{data_dir}",
              #  channel="chrome",
                headless=self.headless,
                slow_mo=150 if self.debug else 0,
                proxy=self.proxy if useProxy and self.proxy else None,
                locale="pt-BR",
                viewport={"width": 1280, "height": 900},
                accept_downloads=False,
                service_workers="block",
                ignore_default_args=["--enable-automation"],
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
            )
            if self.user_agent:
                kwargs["user_agent"] = self.user_agent
            if self.record_har:
                kwargs.update(record_har_path="debug.har", record_har_content="embed")

            try:
                self.context = (
                    await self._playwright.chromium.launch_persistent_context(**kwargs)
                )
                if self.block_resources or self.block_google:
                    await self.context.route("**/*", self._handle_route)
                self._spare_page = self.context.pages[0] if self.context.pages else None
            except Exception:
                # não deixa o Playwright pendurado se o launch falhar
                await self.close()
                raise

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
        """Encerra contexto e driver (chamar no shutdown da app)."""
        if self.context is not None:
            try:
                logger.info("[Browser] Fechando Context")
                await self.context.close()  # grava o perfil em disco
            except Exception as e:
                logger.warning(f"Erro ao fechar contexto: {e}")
            finally:
                self.context = None
                self._spare_page = None

        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception as e:
                logger.warning(f"Erro ao parar Playwright: {e}")
            finally:
                self._playwright = None
                self._spare_page = None

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

    async def new_page(self):
        if self.context is None:
            raise RuntimeError("Contexto Playwright não foi criado")

        page = None
        # reaproveita a aba inicial, se ainda estiver viva
        if self._spare_page is not None and not self._spare_page.is_closed():
            page, self._spare_page = self._spare_page, None
        else:
            self._spare_page = None
            page = await self.context.new_page()

        page.set_default_timeout(settings.selector_timeout_ms)
        page.set_default_navigation_timeout(settings.navigation_timeout_ms)
        return page
