"""End-to-end browser tests: real Playwright browser against real uvicorn backend.

Scenarios:
1. App loads, the first `GET /state` succeeds, status banner reaches "ok".
2. Server can't be reached for `/state` (mocked 503) — status transitions to
   `offline`, not stuck on `syncing`.
"""

from __future__ import annotations

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
    from playwright.sync_api import Browser, Page, Playwright, Route


@pytest.fixture
def browser(playwright_sync: Playwright) -> Iterator[Browser]:
    chromium_root = os.environ.get("CHROMIUM_HEADLESS_SHELL", "")
    executable = str(Path(chromium_root) / "chrome-linux" / "headless_shell") if chromium_root else None
    b = playwright_sync.chromium.launch(
        headless=True,
        executable_path=executable,
        # Flags needed for containerized/RBE environments (no user namespace,
        # /dev/shm may be tiny).
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


def test_initial_state_fetch_completes(page: Page, casino_server: str) -> None:
    """App loads, /state succeeds, banner reaches 'ok'."""
    logs = _attach_logs(page)

    state_responses: list[dict] = []
    page.on(
        "response", lambda r: state_responses.append({"url": r.url, "status": r.status}) if "/state" in r.url else None
    )

    page.goto(casino_server)
    print(f"\n[e2e] page loaded, title={page.title()!r}")

    # ok banner appears once `GET /state` resolves successfully.
    page.wait_for_selector("[data-testid='sync-icon-ok']", state="visible", timeout=30_000)
    print(f"[e2e] sync ok, state_responses={state_responses}, logs={logs}")

    assert page.locator("[data-testid='sync-banner-offline']").count() == 0, (
        f"offline banner showing after first state fetch\nstate_responses={state_responses}\nlogs={logs}"
    )


def test_state_5xx_shows_offline_not_stuck_syncing(page: Page, casino_server: str) -> None:
    """When `GET /state` returns 5xx, the banner transitions to `offline`
    rather than remaining stuck on `syncing`."""
    logs = _attach_logs(page)

    def handle_state(route: Route) -> None:
        route.fulfill(status=503, body="boom")

    page.route("**/state", handle_state)
    page.goto(casino_server)
    print(f"\n[e2e] page loaded for 5xx test, title={page.title()!r}")

    page.wait_for_selector("[data-testid='sync-banner-offline']", state="visible", timeout=30_000)
    print(f"[e2e] offline banner appeared, logs={logs}")
    assert page.locator("[data-testid='sync-banner-syncing']").count() == 0, (
        f"syncing banner still present\nlogs={logs}"
    )


if __name__ == "__main__":
    pytest_bazel.main()
