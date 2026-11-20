"""Real Chromium checks for the experimental CDP context's traffic boundaries."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import pytest_bazel
from playwright.sync_api import BrowserContext, Error, Playwright, Route, sync_playwright
from websockets.sync.server import ServerConnection, serve

from platform_sessions.provider.cdp_browser import CdpBrowser
from platform_sessions.provider.interactive_browser import InteractiveBrowserRuntime
from testing.browser.launcher import CONTAINER_BASE_BROWSER_ARGS, chromium_executable


@pytest.fixture
def playwright_sync() -> Iterator[Playwright]:
    with sync_playwright() as playwright:
        yield playwright


@pytest.fixture
def context(playwright_sync: Playwright) -> Iterator[BrowserContext]:
    with (
        playwright_sync.chromium.launch(
            executable_path=chromium_executable(), args=CONTAINER_BASE_BROWSER_ARGS
        ) as browser,
        browser.new_context() as context,
    ):
        yield context


@pytest.fixture
def upstream() -> Iterator[tuple[str, list[str]]]:
    hits: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            hits.append(self.path)
            body = b"<!doctype html><link rel='icon' href='data:,'>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", hits
        finally:
            server.shutdown()
            worker.join(timeout=5)


def guarded_browser(playwright: Playwright, profile: Path) -> CdpBrowser:
    return CdpBrowser(
        playwright=playwright,
        profile_dir=profile,
        display=":999",
        options={"service_workers": "block"},
        timeout_seconds=2,
    )


def test_provider_must_explicitly_continue_allowed_requests(
    playwright_sync: Playwright, context: BrowserContext, upstream: tuple[str, list[str]], tmp_path: Path
) -> None:
    origin, hits = upstream
    guarded_browser(playwright_sync, tmp_path).guard_context(context)
    page = context.new_page()
    page.route("**/*", lambda route: route.fallback())
    with pytest.raises(Error, match="net::ERR"):
        page.goto(origin + "/fallback")
    assert hits == []
    page.close()
    page = context.new_page()
    page.route(origin + "/explicit", lambda route: route.continue_())
    page.goto(origin + "/explicit")
    assert hits == ["/explicit"]


def test_only_admitted_viewer_page_can_use_network(
    playwright_sync: Playwright, context: BrowserContext, upstream: tuple[str, list[str]], tmp_path: Path
) -> None:
    origin, hits = upstream
    browser = InteractiveBrowserRuntime(
        profile_dir=tmp_path, owner_token="fake-owner-secret-for-browser-tests-1234567890"
    )
    guarded_browser(playwright_sync, tmp_path).guard_context(context)
    viewer = context.new_page()
    viewer.route("**/*", browser._viewer_request)
    background = context.new_page()
    browser._viewer_connected = True
    browser._viewer_network_allowed = True
    viewer.goto(origin + "/viewer")
    with pytest.raises(Error, match="net::ERR"):
        background.goto(origin + "/background")
    browser._viewer_network_allowed = False
    with pytest.raises(Error, match="net::ERR"):
        viewer.goto(origin + "/disconnected")
    assert hits == ["/viewer"]


def test_navigation_before_page_initialization_is_denied(
    playwright_sync: Playwright, context: BrowserContext, upstream: tuple[str, list[str]], tmp_path: Path
) -> None:
    origin, hits = upstream
    guarded_browser(playwright_sync, tmp_path).guard_context(context)
    frameless: list[str] = []

    def observe(route: Route) -> None:
        with pytest.raises(Error, match="before the frame is created"):
            _ = route.request.frame
        frameless.append(route.request.url)
        route.fallback()

    context.route("**/*", observe)
    browser = context.browser
    assert browser is not None
    session = browser.new_browser_cdp_session()
    contexts = session.send("Target.getBrowserContexts")["browserContextIds"]
    assert len(contexts) == 1
    with context.expect_event("requestfailed", predicate=lambda request: request.url == origin + "/restored"):
        session.send("Target.createTarget", {"url": origin + "/restored", "browserContextId": contexts[0]})
    assert frameless == [origin + "/restored"]
    assert hits == []


def test_service_worker_registration_is_blocked(
    playwright_sync: Playwright, context: BrowserContext, upstream: tuple[str, list[str]], tmp_path: Path
) -> None:
    origin, hits = upstream
    guarded_browser(playwright_sync, tmp_path).guard_context(context)
    page = context.new_page()
    page.route(origin + "/allowed", lambda route: route.continue_())
    page.goto(origin + "/allowed")
    assert page.evaluate(
        "async () => { try { await navigator.serviceWorker.register('/worker.js'); return false; }"
        "catch { return true; } }"
    )
    assert context.service_workers == []
    assert hits == ["/allowed"]


def test_background_websocket_is_blocked(
    playwright_sync: Playwright, context: BrowserContext, upstream: tuple[str, list[str]], tmp_path: Path
) -> None:
    origin, hits = upstream
    guarded_browser(playwright_sync, tmp_path).guard_context(context)
    page = context.new_page()
    page.route(origin + "/allowed", lambda route: route.continue_())
    page.goto(origin + "/allowed")
    page.evaluate(
        "url => {globalThis.socketOpened = false; const socket = new WebSocket(url);"
        "socket.onopen = () => {globalThis.socketOpened = true; socket.close();}; }",
        origin.replace("http://", "ws://") + "/socket",
    )
    page.wait_for_function("globalThis.socketOpened", timeout=5000)
    assert hits == ["/allowed"]


def test_existing_service_worker_storage_is_refused_before_process_start(
    playwright_sync: Playwright, tmp_path: Path
) -> None:
    (tmp_path / "Default" / "Service Worker" / "Database").mkdir(parents=True)
    with pytest.raises(ValueError, match="service-worker storage"), guarded_browser(playwright_sync, tmp_path):
        pytest.fail("profile with worker storage was launched")


def test_viewer_websocket_connects_only_during_admission(
    playwright_sync: Playwright, context: BrowserContext, tmp_path: Path
) -> None:
    connections: list[str] = []

    def echo(connection: ServerConnection) -> None:
        connections.append("connected")
        assert connection.recv(timeout=5) == "ping"
        connection.send("pong")

    browser = InteractiveBrowserRuntime(
        profile_dir=tmp_path, owner_token="fake-owner-secret-for-browser-tests-1234567890"
    )
    guarded_browser(playwright_sync, tmp_path).guard_context(context)
    page = context.new_page()
    page.route_web_socket("**/*", browser._viewer_websocket)
    browser._viewer_page = page
    browser._viewer_connected = True
    browser._viewer_network_allowed = True
    with serve(echo, "127.0.0.1", 0) as server:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            port = server.socket.getsockname()[1]
            page.evaluate(
                "url => { const socket = new WebSocket(url); socket.onopen = () => socket.send('ping');"
                "socket.onmessage = event => {globalThis.reply = event.data;}; }",
                f"ws://127.0.0.1:{port}/viewer",
            )
            page.wait_for_function("globalThis.reply === 'pong'", timeout=5000)
            browser._viewer_network_allowed = False
            page.evaluate(
                "url => { const socket = new WebSocket(url);"
                "socket.onopen = () => {globalThis.deniedSocketOpened = true; socket.close();}; }",
                f"ws://127.0.0.1:{port}/after-disconnect",
            )
            page.wait_for_function("globalThis.deniedSocketOpened", timeout=5000)
            assert connections == ["connected"]
        finally:
            server.shutdown()
            thread.join(timeout=5)


if __name__ == "__main__":
    pytest_bazel.main()
