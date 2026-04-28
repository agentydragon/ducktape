"""End-to-end browser tests: real Playwright browser against real uvicorn backend.

Scenarios:
1. App loads, first WebSocket sync completes, syncing banner disappears.
2. Server sends a malformed Yjs update over WebSocket — status transitions to
   `offline` (not stuck on `syncing`), exercising the Y.applyUpdate error path.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import pytest_bazel
import uvicorn

from util.bazel.runfiles import get_required_path
from util.net import pick_free_port
from x.auragon_study_casino.app import create_app
from x.auragon_study_casino.config import Settings

if TYPE_CHECKING:
    from playwright.sync_api import Browser, Page, Playwright, WebSocketRoute


@pytest.fixture
def browser(playwright_sync: Playwright) -> Iterator[Browser]:
    chromium_root = os.environ.get("CHROMIUM_HEADLESS_SHELL", "")
    executable = str(Path(chromium_root) / "chrome-linux" / "headless_shell") if chromium_root else None
    b = playwright_sync.chromium.launch(
        headless=True,
        executable_path=executable,
        # Flags needed for containerized/RBE environments (no user namespace,
        # /dev/shm may be tiny). Without these, IndexedDB can fail to open.
        args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
    )
    try:
        yield b
    finally:
        b.close()


@pytest.fixture
def page(browser: Browser) -> Iterator[Page]:
    context = browser.new_context()
    pg = context.new_page()
    try:
        yield pg
    finally:
        try:
            pg.close()
        finally:
            context.close()


@pytest.fixture
def casino_server(tmp_path: Path) -> Iterator[str]:
    frontend_dist = get_required_path("_main/x/auragon_study_casino/frontend/dist/index.html").parent
    settings = Settings(data_dir=tmp_path, frontend_dist_dir=frontend_dist)
    app = create_app(settings)
    port = pick_free_port("127.0.0.1")
    cfg = uvicorn.Config(app=app, host="127.0.0.1", port=port, log_level="warning", loop="asyncio")
    server = uvicorn.Server(cfg)
    t = threading.Thread(target=server.run, name="casino-uvicorn-e2e", daemon=True)
    t.start()
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and not server.started:
        time.sleep(0.05)
    if not server.started:
        server.should_exit = True
        t.join(timeout=5.0)
        raise RuntimeError("backend did not start within 10s")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        t.join(timeout=5.0)


def _attach_logs(page: Page) -> list[str]:
    logs: list[str] = []
    page.on("pageerror", lambda e: logs.append(f"pageerror: {e}\n{getattr(e, 'stack', '')}"))
    page.on("console", lambda m: logs.append(f"[{m.type}] {m.text}"))
    return logs


def test_initial_sync_completes(page: Page, casino_server: str) -> None:
    """App loads, performs the first /sync round-trip, and clears the syncing banner."""
    logs = _attach_logs(page)

    sync_responses: list[dict] = []
    page.on(
        "response", lambda r: sync_responses.append({"url": r.url, "status": r.status}) if "/sync" in r.url else None
    )

    page.goto(casino_server)
    print(f"\n[e2e] page loaded, title={page.title()!r}")

    # The syncing banner renders from the initial JS state (kind: "syncing"),
    # so it should be visible immediately after React mounts.
    page.wait_for_selector("[data-testid='sync-banner-syncing']", state="visible", timeout=15_000)
    print(f"[e2e] syncing banner appeared, logs so far: {logs}")

    # Wait for the syncing banner to go away (sync round-trip complete or failed).
    # 30 s gives plenty of time on slow RBE workers: IDB init + 200ms debounce +
    # first uvicorn request cold-start + SQLite init.
    page.wait_for_selector("[data-testid='sync-banner-syncing']", state="detached", timeout=30_000)
    print(f"[e2e] sync completed, sync_responses={sync_responses}, logs={logs}")

    assert page.locator("[data-testid='sync-banner-offline']").count() == 0, (
        f"offline banner showing after sync\nsync_responses={sync_responses}\nlogs={logs}"
    )


def test_corrupt_server_update_shows_offline_not_stuck_syncing(page: Page, casino_server: str) -> None:
    """When the WebSocket server sends bytes that are not valid Yjs, status should
    transition to `offline` rather than remaining stuck on `syncing`.
    Exercises the try/catch around Y.applyUpdate in sync.js."""
    logs = _attach_logs(page)

    def handle_ws(ws_route: WebSocketRoute) -> None:
        # Mock the WebSocket: respond to every client message with a corrupt
        # "accepted" payload so Y.applyUpdate always fails.
        def on_message(message: str | bytes) -> None:
            ws_route.send(
                json.dumps(
                    {
                        "type": "accepted",
                        "update_b64": "bm90LXlqcy1kYXRh",  # b64("not-yjs-data")
                        "state_vector_b64": "",
                    }
                )
            )

        ws_route.on_message(on_message)

    page.route_web_socket("**/ws", handle_ws)
    page.goto(casino_server)
    print(f"\n[e2e] page loaded for corrupt test, title={page.title()!r}")

    # Wait for syncing banner to appear first (confirms React mounted and WS connected).
    page.wait_for_selector("[data-testid='sync-banner-syncing']", state="visible", timeout=15_000)
    print(f"[e2e] syncing banner appeared in corrupt test, logs so far: {logs}")

    page.wait_for_selector("[data-testid='sync-banner-offline']", state="visible", timeout=30_000)
    print(f"[e2e] offline banner appeared, logs={logs}")
    assert page.locator("[data-testid='sync-banner-syncing']").count() == 0, (
        f"syncing banner still present\nlogs={logs}"
    )


if __name__ == "__main__":
    pytest_bazel.main()
