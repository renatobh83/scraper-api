import asyncio
import logging
import os
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

from playwright.async_api import BrowserContext, async_playwright

from app.config import settings

logger = logging.getLogger("uvicorn.error")


class BrowserManager:
    """Gerencia instâncias isoladas do navegador Chromium via Playwright Async API.

    Responsabilidade:
    - Criar e reutilizar contextos persistentes (BrowserContext) por perfil/proxy.
    - Bloquear recursos desnecessários (imagens, fontes, rastreadores Google) para economizar RAM e CPU.
    - Fechar contextos de forma limpa quando houver falhas ou encerramento da API.
    """

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

        # Configurações via variáveis de ambiente
        self.debug = os.getenv("DEBUG_BROWSER", "0") == "1"
        self.proxy = self._load_proxy()
        self.block_google = os.getenv("BLOCK_GOOGLE", "1") == "1"
        self.block_resources = os.getenv("BLOCK_RESOURCES", "1") == "1"
        self.headless = os.getenv("BROWSER_HEADLESS", "1") == "1"
        self.record_har = os.getenv("RECORD_HAR", "0") == "1"
        self.user_agent = os.getenv("BROWSER_USER_AGENT") or None

    def _context_key(self, use_proxy: bool, profile: str) -> str:
        """Gera a chave única para identificar o contexto no dicionário."""
        return f"{profile}:{'proxy' if use_proxy else 'noproxy'}"

    async def _get_lock(self, key: str) -> asyncio.Lock:
        """Garante a criação thread-safe de um Lock por chave de contexto."""
        async with self._global_lock:
            if key not in self._locks:
                self._locks[key] = asyncio.Lock()
            return self._locks[key]

    def _is_context_valid(self, ctx: BrowserContext) -> bool:
        """Verifica se o BrowserContext e o Browser subjacente continuam ativos."""
        if ctx is None:
            return False
        # No Playwright Python, o objeto browser do contexto pode ser verificado ou capturado por exceção
        try:
            return ctx.browser is None or ctx.browser.is_connected()
        except Exception:
            return False

    async def start(self, data_dir: str, useProxy: bool = True) -> str:
        key = self._context_key(useProxy, data_dir)

        # 1. Checagem rápida antes de adquirir o lock
        if key in self._contexts:
            ctx = self._contexts[key]
            if self._is_context_valid(ctx):
                return key
            else:
                logger.warning(
                    f"[Browser] Contexto {key} inválido/fechado detectado. Limpando..."
                )
                await self.close_context(key)

        lock = await self._get_lock(key)

        async with lock:
            # 2. Double-check locking
            if key in self._contexts:
                ctx = self._contexts[key]
                if self._is_context_valid(ctx):
                    return key
                else:
                    await self.close_context(key)

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
                    "--disable-dev-shm-usage",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--max-sockets-per-group=4",
                    "--renderer-process-limit=1",
                ],
            )

            if self.user_agent:
                kwargs["user_agent"] = self.user_agent

            if self.record_har:
                kwargs.update(
                    record_har_path=f"debug_{key.replace(':', '_')}.har",
                    record_har_content="embed",
                )

            context = await self._playwright.chromium.launch_persistent_context(
                **kwargs
            )

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
        """Inicia um novo BrowserContext persistent se ainda não existir.

        Retorna a chave do contexto gerado.
        """
        key = self._context_key(useProxy, data_dir)

        if key in self._contexts:
            ctx = self._contexts[key]
            if ctx.is_connected():  # type: ignore
                return key
        else:
            # Se foi fechado externamente, limpa para recriar
            await self.close_context(key)

        lock = await self._get_lock(key)

        async with lock:
            # Double-check locking pattern para evitar múltiplas instanciações
            if key in self._contexts:
                ctx = self._contexts[key]
                if ctx.is_connected():  # type: ignore
                    return key
                else:
                    await self.close_context(key)

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
                    "--disable-dev-shm-usage",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--max-sockets-per-group=4",
                    "--renderer-process-limit=1",
                ],
            )

            if self.user_agent:
                kwargs["user_agent"] = self.user_agent

            if self.record_har:
                kwargs.update(
                    record_har_path=f"debug_{key.replace(':', '_')}.har",
                    record_har_content="embed",
                )

            context = await self._playwright.chromium.launch_persistent_context(
                **kwargs
            )

            # Oculta evidências do Playwright no objeto navigator
            await context.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'languages', { get: () => ['pt-BR', 'pt', 'en'] });
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                window.chrome = { runtime: {} };
            """)

            # Aplica filtro de requisições de rede se configurado
            if self.block_resources or self.block_google:
                await context.route("**/*", self._handle_route)

            self._contexts[key] = context
            self._spare_pages[key] = context.pages[0] if context.pages else None

            return key

    async def _handle_route(self, route) -> None:
        """Intercepta e cancela requisições desnecessárias para economizar largura de banda e RAM."""
        hostname = urlsplit(route.request.url).hostname or ""

        # Nunca bloquear Cloudflare para permitir resolução de desafios Turnstile/Bot Fight
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

    async def new_page(self, key: str):
        ctx = self._contexts.get(key)
        if ctx is None:
            raise RuntimeError(f"Contexto '{key}' não iniciado")

        page = None
        spare = self._spare_pages.get(key)

        if spare is not None and not spare.is_closed():
            page = spare
        self._spare_pages[key] = None

        if page is None:
            try:
                page = await ctx.new_page()
            except Exception as exc:
                # Se o contexto foi fechado antes do new_page, remove a referência corrompida
                await self.close_context(key)
                raise RuntimeError(
                    f"Falha ao abrir página no contexto '{key}': {exc}"
                ) from exc

        page.set_default_timeout(settings.selector_timeout_ms)
        page.set_default_navigation_timeout(settings.navigation_timeout_ms)
        return page

    async def close_context(self, key: str) -> None:
        """Encerra um contexto específico e libera os recursos/sockets associados."""
        ctx = self._contexts.pop(key, None)
        self._spare_pages.pop(key, None)

        if ctx is None:
            return

        try:
            await ctx.close()
            logger.info(f"[Browser] Contexto fechado: {key}")
        except Exception as e:
            logger.warning(f"[Browser] Erro fechando contexto {key}: {e}")

    async def close_all(self) -> None:
        """Encerra todos os contextos ativos e desliga a instância do Playwright."""
        for key, ctx in list(self._contexts.items()):
            try:
                await ctx.close()
            except Exception as e:
                logger.warning(f"[Browser] Erro fechando {key}: {e}")

        self._contexts.clear()
        self._spare_pages.clear()
        self._locks.clear()

        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception as e:
                logger.warning(f"[Browser] Erro parando Playwright: {e}")
            finally:
                self._playwright = None

    async def close(self) -> None:
        await self.close_all()

    @staticmethod
    def _load_proxy() -> dict[str, str] | None:
        """Lê variáveis de ambiente e constrói o dicionário de configuração de Proxy."""
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
        """Verifica se o hostname pertence aos domínios bloqueados do Google."""
        if not hostname:
            return False
        hostname = hostname.lower().rstrip(".")
        return any(
            hostname == domain or hostname.endswith(f".{domain}")
            for domain in cls.BLOCKED_GOOGLE_DOMAINS
        )
