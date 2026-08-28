"""End-to-end browser tests: real Playwright browser against real uvicorn backend.

Scenarios:
1. App loads, the first `GET /state` succeeds, status banner reaches "ok".
2. Server can't be reached for `/state` (mocked 503) — status transitions to
   `offline`, not stuck on `syncing`.
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from collections.abc import Iterator
from typing import TYPE_CHECKING, cast

import pytest
import pytest_bazel

from study_casino.app import create_app
from study_casino.changelog import LATEST_CHANGELOG_ID
from study_casino.config import Settings
from util.bazel.runfiles import get_required_path
from util.testing.asgi import serve_app_sync

# pytest_plugins loads util.playwright by name; gazelle cannot see the dependency.
# gazelle:include_dep //util:playwright

pytest_plugins = ("util.playwright",)

if TYPE_CHECKING:
    from playwright.sync_api import Page, Route

# `browser` (with the shared container-safe flags) and `page` come from
# util.playwright via conftest.py.


@pytest.fixture
def casino_server(db_url: str) -> Iterator[str]:
    frontend_dist = get_required_path("_main/study_casino/frontend/dist/index.html").parent
    app = create_app(Settings(database_url=db_url, frontend_dist_dir=frontend_dist))
    with serve_app_sync(app) as base_url:
        yield base_url


def _attach_logs(page: Page) -> list[str]:
    logs: list[str] = []
    page.on("pageerror", lambda e: logs.append(f"pageerror: {e}\n{getattr(e, 'stack', '')}"))
    page.on("console", lambda m: logs.append(f"[{m.type}] {m.text}"))
    return logs


def _post_json(base_url: str, path: str, payload: dict[str, object]) -> dict[str, object]:
    req = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert resp.status == 200
        decoded = json.loads(resp.read().decode())
        assert isinstance(decoded, dict)
        return cast(dict[str, object], decoded)


def _ack_changelog(base_url: str) -> None:
    """Ack the changelog so the "what's new" modal doesn't intercept clicks."""
    _post_json(
        base_url,
        "/actions/changelog/ack",
        {"client_action_id": f"test.changelog:{time.time_ns()}", "last_id": LATEST_CHANGELOG_ID},
    )


def _seed_credits(base_url: str, credits: int) -> None:
    _post_json(
        base_url,
        "/actions/import",
        {
            "client_action_id": f"test.import:{time.time_ns()}",
            "data": {"credits": credits, "tokens": 0, "sessions": [], "prizes": [], "prizeLog": []},
        },
    )
    _ack_changelog(base_url)


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


def test_roulette_spin_sets_finite_wheel_transform(page: Page, casino_server: str) -> None:
    """Roulette consumes the nested action result and animates toward a real pocket."""
    _seed_credits(casino_server, 100)
    logs = _attach_logs(page)

    page.goto(casino_server)
    page.wait_for_selector("[data-testid='sync-icon-ok']", state="visible", timeout=30_000)
    page.get_by_role("button", name="casino").click()
    page.get_by_role("button", name=re.compile(r"^Spin . 10 cr$")).click()

    page.wait_for_function(
        """
        () => {
          const wheel = document.querySelector('svg[viewBox="-150 -150 300 300"]');
          const transform = wheel?.style.transform || "";
          return transform.startsWith("rotate(") && transform !== "rotate(0deg)" && !transform.includes("NaN");
        }
        """,
        timeout=5_000,
    )

    assert not [line for line in logs if line.startswith("pageerror:")], f"browser errors during roulette spin\n{logs}"


def test_changelog_modal_blocks_until_acked_and_ack_persists(page: Page, casino_server: str) -> None:
    """A fresh user sees the "what's new" modal; "Got it" dismisses it and the
    ack survives a reload (server-side cursor, not local state)."""
    logs = _attach_logs(page)

    page.goto(casino_server)
    page.wait_for_selector("[data-testid='sync-icon-ok']", state="visible", timeout=30_000)
    modal_title = page.get_by_text("What's new")
    modal_title.wait_for(state="visible", timeout=10_000)

    page.get_by_role("button", name="Got it").click()
    modal_title.wait_for(state="hidden", timeout=10_000)

    page.reload()
    page.wait_for_selector("[data-testid='sync-icon-ok']", state="visible", timeout=30_000)
    assert page.get_by_text("What's new").count() == 0, "changelog modal reappeared after ack + reload"
    assert not [line for line in logs if line.startswith("pageerror:")], f"browser errors during changelog ack\n{logs}"


def test_stop_and_save_shows_award_toast(page: Page, casino_server: str) -> None:
    """Completing a live session via Stop & Save surfaces the server-computed
    award toast (credits + streak multiplier + daily bonus), and dismissing it
    removes it."""
    _ack_changelog(casino_server)
    logs = _attach_logs(page)

    # Backdate an in-progress 6-minute session in localStorage (where the
    # production timer lives) so the completed session crosses the 5-minute
    # daily-bonus threshold and earns a nonzero award.
    page.add_init_script(
        """
        if (!window.localStorage.getItem("casino:active_session")) {
          window.localStorage.setItem("casino:active_session", JSON.stringify({
            subject: "Biochemistry",
            startTime: Date.now() - 6 * 60 * 1000,
            paused: false,
            pausedDuration: 0,
            pauseStartedAt: null,
          }));
        }
        """
    )
    page.goto(casino_server)
    page.wait_for_selector("[data-testid='sync-icon-ok']", state="visible", timeout=30_000)

    page.get_by_role("button", name="Stop & Save").click()

    # Server-computed award: 6 minutes → base credits + first-5-minutes daily
    # bonus at streak day 1. (Match "daily bonus" inside the toast subtitle —
    # the always-visible streak strip also mentions the daily bonus.)
    toast_credits = page.get_by_text(re.compile(r"^\+.*credits$"))
    toast_credits.wait_for(state="visible", timeout=10_000)
    page.get_by_text(re.compile(r"6m studied · 1-day streak .*daily bonus")).wait_for(state="visible", timeout=5_000)

    page.get_by_role("button", name="Dismiss").click()
    toast_credits.wait_for(state="hidden", timeout=5_000)

    assert not [line for line in logs if line.startswith("pageerror:")], f"browser errors during stop-and-save\n{logs}"


def test_slots_spin_does_not_throw_on_server_action_response(page: Page, casino_server: str) -> None:
    """Slots consumes the nested action result before mapping returned symbols."""
    _seed_credits(casino_server, 100)
    logs = _attach_logs(page)

    page.goto(casino_server)
    page.wait_for_selector("[data-testid='sync-icon-ok']", state="visible", timeout=30_000)
    page.get_by_role("button", name="casino").click()
    page.get_by_role("button", name="slots").click()
    page.get_by_role("button", name=re.compile(r"^Spin . 5 cr$")).click()
    page.wait_for_timeout(500)

    assert not [line for line in logs if line.startswith("pageerror:")], f"browser errors during slots spin\n{logs}"


if __name__ == "__main__":
    pytest_bazel.main()
