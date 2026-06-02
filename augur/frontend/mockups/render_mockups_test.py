"""Render static design mockups to PNGs via the visual-test browser.

This is NOT a golden test — it produces images for design review (no comparison). Fetch them from
the BuildBuddy invocation's undeclared outputs:

    bbr test //augur/frontend:mockups_render --nocache_test_results
    INV=$(cat ~/.cache/bbr/last_invocation_id)
    bbapi artifact list "$INV"
    bbapi artifact download "$INV" mock-scenario-table.png -o augur/frontend/mockups/mock-scenario-table.png

Rendered copies are committed next to `mockups.html` so they show up in the PR without running the
test; regenerate them with the commands above whenever the mockups change. The render is
deterministic (frozen clock + Inter font + fixed synthetic chart data), so the PNGs are stable.

Each top-level `<section id="mock-...">` in `mockups.html` is screenshotted into its own PNG, so
new mockups only need a new section + an entry in `_SECTIONS`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest_bazel

from util.bazel.runfiles import get_required_path
from util.testing.frontend_visual import (
    deterministic_browser_context,
    deterministic_style,
    launch_deterministic_browser,
)
from util.testing.undeclared_outputs import undeclared_outputs_dir

pytest_plugins = ("util.playwright",)

if TYPE_CHECKING:
    from playwright.sync_api import Playwright

_FROZEN_NOW_MS = 1_779_768_000_000  # 2026-05-15T12:00:00Z (matches the visual goldens).
_SECTIONS = ("mock-scenario-table", "mock-chart-lines", "mock-chart-candles")


def test_render_mockups(playwright_sync: Playwright) -> None:
    html = get_required_path("_main/augur/frontend/mockups/mockups.html").read_text()
    out_dir = undeclared_outputs_dir()
    browser = launch_deterministic_browser(playwright_sync)
    try:
        context = deterministic_browser_context(
            browser, viewport={"width": 1120, "height": 1000}, frozen_now_ms=_FROZEN_NOW_MS
        )
        page = context.new_page()
        page.set_content(html, wait_until="load")
        page.add_style_tag(content=deterministic_style())
        page.wait_for_function("() => window.__mockupsReady === true", timeout=10_000)
        page.evaluate("() => document.fonts.ready.then(() => true)")
        for section in _SECTIONS:
            locator = page.locator(f"#{section}")
            locator.wait_for(state="visible", timeout=10_000)
            locator.screenshot(path=str(out_dir / f"{section}.png"))
        context.close()
    finally:
        browser.close()


if __name__ == "__main__":
    pytest_bazel.main()
