"""Render-health checks + PR-visuals publication for each casino view.

Every (view, viewport) case boots the real server, waits for load-bearing DOM,
captures once, and fails on any browser page error. The rendered PNGs plus a
`visual-review.json` manifest go to undeclared outputs,
where trusted CI (`devinfra/pr_visuals/publisher.py` via the "Publish PR
visuals" workflow) publishes them as a browsable bundle, diffs them against the
merge-base baseline, and comments on the PR.

There is no checked-in pixel golden — pixel changes are reviewed on the PR's
visual-review page, not gated in CI (see
devinfra/pr_visuals/plans/goldens_to_pr_visuals.md).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager

import pytest
import pytest_bazel
from playwright.async_api import Playwright, ViewportSize

from study_casino.app import create_app
from study_casino.changelog import LATEST_CHANGELOG_ID
from study_casino.config import Settings
from util.bazel.runfiles import get_required_path
from util.testing.asgi import serve_app_sync
from util.testing.frontend_visual import deterministic_browser_context, stability_style
from util.testing.postgres_fixtures import start_postgres_container
from util.testing.undeclared_outputs import undeclared_outputs_dir
from util.testing.visual_capture import VisualPage

# pytest_plugins loads util.playwright by name; gazelle cannot see the dependency.
# gazelle:include_dep //util:playwright
# gazelle:include_dep //util/testing:visual_fixtures

pytest_plugins = ("util.playwright", "util.testing.visual_fixtures")


# Two viewports per case: a desktop width that exercises the two-column casino
# layouts and a mobile width that should show the single-column responsive
# variant. The mobile width matches iPhone 14 logical CSS pixels (most narrow
# phones land between 360 and 420 CSS px).
DESKTOP_VIEWPORT: ViewportSize = {"width": 1280, "height": 900}
MOBILE_VIEWPORT: ViewportSize = {"width": 390, "height": 844}

# Frozen wall-clock so the active-session timer, today's totals, and any
# Date.now()-driven UI render identically across runs.
FROZEN_NOW_MS = 1_779_768_000_000  # 2026-05-15T12:00:00Z.


@asynccontextmanager
async def casino_view(
    playwright: Playwright, casino_server: str, viewport: ViewportSize, query: str
) -> AsyncIterator[VisualPage]:
    async with deterministic_browser_context(
        playwright, viewport=viewport, frozen_now_ms=FROZEN_NOW_MS, color_scheme="dark"
    ) as context:
        page = await context.new_page()
        view = VisualPage(page, output_dir=undeclared_outputs_dir(), title="Study Casino views", output_suffix="")
        await page.goto(f"{casino_server}/{query}", wait_until="networkidle", timeout=30_000)
        await page.add_style_tag(content=stability_style())
        try:
            yield view
        finally:
            view.errors.assert_none(context=query)


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
    # the modal has its own harness-based visual test (//study_casino/frontend:visual, scenario `changelog`).
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


@pytest.mark.parametrize("viewport", [DESKTOP_VIEWPORT, MOBILE_VIEWPORT], ids=["desktop", "mobile"])
@pytest.mark.parametrize(
    ("query", "visible_text"),
    [
        ("?view=study", "Today"),
        ("?view=casino&game=roulette", "ROULETTE"),
        ("?view=casino&game=blackjack", "BLACKJACK"),
        ("?view=casino&game=slots", "SLOTS"),
        ("?view=prizes", "The Vault"),
        ("?view=stats", "The Ledger"),
    ],
    ids=["study", "casino_roulette", "casino_blackjack", "casino_slots", "prizes", "stats"],
)
async def test_casino_views_render(
    playwright: Playwright, casino_server: str, viewport: ViewportSize, query: str, visible_text: str, capture_name: str
) -> None:
    async with casino_view(playwright, casino_server, viewport, query) as view:
        await view.page.get_by_text(visible_text).first.wait_for(state="visible", timeout=15_000)
        await view.capture(capture_name, full_page=True, animations="disabled", scale="css")


if __name__ == "__main__":
    pytest_bazel.main()
