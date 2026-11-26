"""Own the persistent browser and the owner's tabs."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import BrowserContext, Error as PlaywrightError, Page, Route
from starlette.websockets import WebSocket

from attendant.runtime import Operation, Provider, Reply, SessionError, Tab, valid_target
from platform_sessions.provider.interactive_browser import InteractiveBrowserRuntime


logger = logging.getLogger(__name__)


class Session:
    """All methods run on the Playwright thread, including status checks and dispatch."""

    def __init__(self, context: BrowserContext, providers: Sequence[Provider]):
        self.context = context
        self.providers = {provider.name: provider for provider in providers}
        self.pages: dict[str, Page] = {}
        self.selected: str | None = None

    def open(self, name: str) -> None:
        provider = self.providers[name]
        page = self.pages.get(name)
        if (
            page is not None
            and not page.is_closed()
            and page.url not in {"about:blank", "chrome-error://chromewebdata/"}
        ):
            self.selected = name
            page.bring_to_front()
            return
        if page is None or page.is_closed():
            page = self.context.new_page()
            self.pages[name] = page
            policy = getattr(provider, "allow_navigation", None)

            def guard(route: Route) -> None:
                if policy is None or policy(route.request.method, route.request.url):
                    route.continue_()
                else:
                    route.abort()

            page.route("**/*", guard)
            page.route_web_socket("**/*", lambda route: route.connect_to_server())
        self.selected = name
        page.bring_to_front()
        page.goto(provider.login_url, wait_until="commit", timeout=20000)

    def _provider_page(self, provider: Provider) -> Page | None:
        # The owner may have navigated a different tab or restored one from the profile.
        opened = self.pages.get(provider.name)
        for page in (opened, *reversed(self.context.pages)):
            if page is None or page.is_closed():
                continue
            url = urlsplit(page.url)
            if f"{url.scheme}://{url.netloc}" == provider.origin:
                return page
        return None

    def status(self, name: str) -> str:
        provider = self.providers[name]
        try:
            page = self._provider_page(provider)
            with Tab.open(self.context, provider.origin) as tab:
                provider.check_session(tab, page)
        except SessionError as issue:
            return issue.code
        except PlaywrightError:
            return "upstream_error"
        return "ready"

    def request(self, name: str, op: Operation) -> Reply:
        provider = self.providers[name]
        if not valid_target(op.target) or not provider.allows(op):
            raise SessionError("denied")
        logger.info("provider request started provider=%s", name)
        with Tab.open(self.context, provider.origin) as tab:
            return provider.request(tab, op)

    def nudge(self) -> None:
        for name, page in list(self.pages.items()):
            provider = self.providers[name]
            callback = getattr(provider, "nudge", None)
            if callback is None or page.is_closed():
                continue
            url = urlsplit(page.url)
            if f"{url.scheme}://{url.netloc}" == provider.origin:
                with suppress(PlaywrightError):
                    callback(page)


class Host:
    def __init__(self, state_dir: Path, providers: Sequence[Provider], owner_token: str):
        self.state_dir = state_dir
        self.providers = {provider.name: provider for provider in providers}
        if len(self.providers) != len(providers) or not providers:
            raise ValueError("provider names must be unique and nonempty")
        self.browser = InteractiveBrowserRuntime(
            profile_dir=state_dir / "profile",
            owner_token=owner_token,
            launch_mode="cdp",
            launch_options={"service_workers": "block", "args": ["--no-proxy-server", "--restore-last-session"]},
        )
        self.session: Session
        self._automation: asyncio.Task[None] | None = None
        # Keep the browser job queue bounded even if API clients exceed service capacity.
        self._gate = asyncio.Lock()

    async def start(self) -> None:
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        await self.browser.start()
        try:
            self.session = await asyncio.to_thread(
                self.browser.run_sync, lambda ctx: Session(ctx, list(self.providers.values()))
            )
            self._automation = asyncio.create_task(self._run_automation())
        except BaseException:
            await self.browser.close()
            raise

    async def close(self) -> None:
        if self._automation:
            self._automation.cancel()
            with suppress(asyncio.CancelledError):
                await self._automation
        await self.browser.close()

    async def _run_automation(self) -> None:
        while True:
            await asyncio.sleep(20)
            if not self.browser.viewer_connected and not self._gate.locked():
                with suppress(SessionError):
                    await self._run(self.session.nudge)

    async def _run[T, **P](self, callback: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
        if self._gate.locked():
            raise SessionError("busy")
        async with self._gate:

            def invoke(_context: BrowserContext) -> T:
                return callback(*args, **kwargs)

            task = asyncio.create_task(asyncio.to_thread(self.browser.run_sync, invoke, allow_during_viewer=True))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # The browser callback still owns its tab until it returns.
                with suppress(Exception):
                    await task
                raise
            except PlaywrightError, RuntimeError:
                raise SessionError("upstream_error") from None

    async def open(self, name: str) -> None:
        await self._run(self.session.open, name)

    async def status(self, name: str) -> str:
        return await self._run(self.session.status, name)

    async def request(self, name: str, op: Operation) -> Reply:
        def dispatch() -> Reply:
            if self.browser.viewer_connected and self.session.selected == name:
                raise SessionError("busy")
            return self.session.request(name, op)

        return await self._run(dispatch)

    async def websocket(self, websocket: WebSocket) -> None:
        await self.browser.websocket(websocket)
