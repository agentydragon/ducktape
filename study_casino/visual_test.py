"""Render-health checks + PR-visuals publication for each casino view.

Every (view, viewport) case boots the real server, waits for load-bearing DOM,
renders twice to prove determinism, and fails on any browser page error. The
rendered PNGs plus a `visual-review.json` manifest go to undeclared outputs,
where trusted CI (`devinfra/pr_visuals/publisher.py` via the "Publish PR
visuals" workflow) publishes them as a browsable bundle, diffs them against the
merge-base baseline, and comments on the PR.

There is no checked-in pixel golden — pixel changes are reviewed on the PR's
visual-review page, not gated in CI (see
devinfra/pr_visuals/plans/goldens_to_pr_visuals.md).
"""

from __future__ import annotations

import json
import shutil
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import pytest_bazel

from study_casino.app import create_app
from study_casino.changelog import LATEST_CHANGELOG_ID
from study_casino.config import Settings
from util.bazel.runfiles import get_required_path
from util.testing.asgi import serve_app_sync
from util.testing.frontend_visual import deterministic_browser_context, launch_deterministic_browser, stability_style
from util.testing.postgres_fixtures import start_postgres_container
from util.testing.undeclared_outputs import undeclared_outputs_dir
from util.testing.visual_review import retain_review_asset

# pytest_plugins loads util.playwright by name; gazelle cannot see the dependency.
# gazelle:include_dep //util:playwright

pytest_plugins = ("util.playwright",)

if TYPE_CHECKING:
    from playwright.sync_api import Browser, Playwright, ViewportSize


# Two viewports per case: a desktop width that exercises the two-column casino
# layouts and a mobile width that should show the single-column responsive
# variant. The mobile width matches iPhone 14 logical CSS pixels (most narrow
# phones land between 360 and 420 CSS px).
DESKTOP_VIEWPORT: ViewportSize = {"width": 1280, "height": 900}
MOBILE_VIEWPORT: ViewportSize = {"width": 390, "height": 844}

# Frozen wall-clock so the active-session timer, today's totals, and any
# Date.now()-driven UI render identically across runs.
FROZEN_NOW_MS = 1_779_768_000_000  # 2026-05-15T12:00:00Z.


@dataclass(frozen=True)
class Case:
    name: str
    query: str  # appended to "/", e.g. "?view=casino&game=roulette"
    visible_text: str  # selector text that must be visible before screenshotting
    viewport: ViewportSize


def _both_widths(slug: str, query: str, visible_text: str) -> tuple[Case, Case]:
    return (
        Case(name=f"{slug}.desktop", query=query, visible_text=visible_text, viewport=DESKTOP_VIEWPORT),
        Case(name=f"{slug}.mobile", query=query, visible_text=visible_text, viewport=MOBILE_VIEWPORT),
    )


CASES: tuple[Case, ...] = (
    *_both_widths("study", "?view=study", "Today"),
    *_both_widths("casino_roulette", "?view=casino&game=roulette", "ROULETTE"),
    *_both_widths("casino_blackjack", "?view=casino&game=blackjack", "BLACKJACK"),
    *_both_widths("casino_slots", "?view=casino&game=slots", "SLOTS"),
    *_both_widths("prizes", "?view=prizes", "The Vault"),
    *_both_widths("stats", "?view=stats", "The Ledger"),
)


@pytest.fixture
def browser(playwright_sync: Playwright) -> Iterator[Browser]:
    instance = launch_deterministic_browser(playwright_sync)
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture(scope="module")
def casino_server() -> Iterator[str]:
    """uvicorn-backed casino server with a fresh Postgres testcontainer."""
    container = start_postgres_container()
    try:
        host = container.get_container_host_ip()
        port = int(container.get_exposed_port(5432))
        db_url = f"postgresql+psycopg://postgres:postgres@{host}:{port}/postgres"

        frontend_dist = get_required_path("_main/study_casino/frontend/dist/index.html").parent

        # The rendered views want a non-empty UI for prizes/stats. Seed a few
        # sessions and a token balance via the server's own action endpoints
        # right after startup, before the screenshots run.
        settings = Settings(database_url=db_url, frontend_dist_dir=frontend_dist, admin_users={"default"})
        with serve_app_sync(create_app(settings)) as origin:
            _seed_fixture_state(origin)
            yield origin
    finally:
        container.stop()


def _seed_fixture_state(origin: str) -> None:
    """Populate a small fixture state so the rendered views have content.

    Adds two completed past sessions (so Stats / Study panels show data) and
    converts a chunk to tokens (so the Vault shows redeemable balance + the
    casino games have credits to wager).
    """
    _post(
        origin,
        "/actions/session/add-past",
        {
            "client_action_id": "visual-seed-session-1",
            "subject": "Biochem",
            "seconds": 90 * 60,
            "ended_at_ms": FROZEN_NOW_MS - 6 * 3600 * 1000,
        },
    )
    _post(
        origin,
        "/actions/session/add-past",
        {
            "client_action_id": "visual-seed-session-2",
            "subject": "Pharmacology",
            "seconds": 45 * 60,
            "ended_at_ms": FROZEN_NOW_MS - 2 * 3600 * 1000,
        },
    )
    _post(origin, "/actions/convert", {"client_action_id": "visual-seed-convert", "amount": 100})
    # Ack the changelog so the "what's new" modal doesn't cover every view;
    # the modal has its own harness-based visual test (frontend:visual_changelog).
    _post(
        origin, "/actions/changelog/ack", {"client_action_id": "visual-seed-changelog", "last_id": LATEST_CHANGELOG_ID}
    )


def _post(origin: str, path: str, payload: dict) -> None:
    request = urllib.request.Request(
        f"{origin}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        if response.status != 200:
            raise RuntimeError(f"seed {path} failed: HTTP {response.status}")


def _render_case(browser: Browser, origin: str, case: Case, out_dir: Path, suffix: str) -> Path:
    """Render one case; fails on any browser page error (render health)."""
    context = deterministic_browser_context(
        browser, viewport=case.viewport, frozen_now_ms=FROZEN_NOW_MS, color_scheme="dark"
    )
    page = context.new_page()
    page_errors: list[str] = []
    page.on("pageerror", lambda e: page_errors.append(f"{e}\n{getattr(e, 'stack', '')}"))
    try:
        page.goto(f"{origin}/{case.query}", wait_until="networkidle", timeout=30_000)
        page.add_style_tag(content=stability_style())
        page.get_by_text(case.visible_text).first.wait_for(state="visible", timeout=15_000)
        # Force fonts to settle before screenshot.
        page.evaluate("() => document.fonts.ready.then(() => true)")
        actual_path = out_dir / f"{case.name}.{suffix}.png"
        page.screenshot(path=str(actual_path), full_page=True, animations="disabled", caret="hide", scale="css")
        if page_errors:
            raise AssertionError(f"{case.name} raised browser page errors:\n" + "\n".join(page_errors))
        return actual_path
    finally:
        page.close()
        context.close()


@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
def test_casino_views_render(browser: Browser, casino_server: str, tmp_path: Path, case: Case) -> None:
    undeclared_dir = undeclared_outputs_dir()
    first_path = _render_case(browser, casino_server, case, tmp_path, "first")
    second_path = _render_case(browser, casino_server, case, tmp_path, "second")
    if first_path.read_bytes() != second_path.read_bytes():
        shutil.copy(first_path, undeclared_dir / f"{case.name}.first.png")
        shutil.copy(second_path, undeclared_dir / f"{case.name}.second.png")
        raise AssertionError(
            f"{case.name} visual render is not deterministic across reloads; "
            f"inspect {case.name}.first.png and {case.name}.second.png in {undeclared_dir}"
        )

    # Retain the render + visual-review manifest for the PR visual-review
    # publisher (devinfra/pr_visuals/publisher.py) — the pixel-review path.
    retain_review_asset(
        first_path,
        title="Study Casino views",
        label=case.name.replace("_", " ").replace(".", " · "),
        name=f"{case.name}.png",
    )


if __name__ == "__main__":
    pytest_bazel.main()
