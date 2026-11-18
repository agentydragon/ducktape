"""Real Chromium checks for the experimental CDP context's traffic boundaries."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import pytest_bazel
from playwright.sync_api import BrowserContext, Error, Page, Playwright, sync_playwright

from platform_sessions.provider.cdp_browser import CdpBrowser
from util.testing.browser.launcher import CONTAINER_BASE_BROWSER_ARGS, chromium_executable


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


def guarded_browser(playwright: Playwright, profile: Path, allow_viewer_page=lambda _: False) -> CdpBrowser:
    return CdpBrowser(
        playwright=playwright,
        profile_dir=profile,
        display=":999",
        options={"service_workers": "block"},
        timeout_seconds=2,
        allow_viewer_page=allow_viewer_page,
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
    viewer: Page | None = None
    admitted = False
    guarded_browser(playwright_sync, tmp_path, lambda page: admitted and page is viewer).guard_context(context)
    viewer = context.new_page()
    background = context.new_page()
    admitted = True
    viewer.goto(origin + "/viewer")
    with pytest.raises(Error, match="net::ERR"):
        background.goto(origin + "/background")
    admitted = False
    with pytest.raises(Error, match="net::ERR"):
        viewer.goto(origin + "/disconnected")
    assert hits == ["/viewer"]


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


if __name__ == "__main__":
    pytest_bazel.main()
