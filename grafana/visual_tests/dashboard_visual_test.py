"""Render a dashboard with recorded, sanitized datasource responses.

This scenario tests dashboard appearance, not query execution. A dashboard can
provide a custom scenario with real isolated datasources when query behavior matters.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

import pytest
import pytest_bazel
from playwright.async_api import async_playwright, expect

from grafana.visual_tests.recorded import replay_query, validate_fixture
from grafana.visual_tests.visual_browser import capture_dashboard
from grafana.visual_tests.visual_harness import grafana_dashboard, record_stage
from util.bazel.runfiles import get_required_path
from util.testing.frontend_visual import FROZEN_NOW_MS, deterministic_browser_context
from util.testing.undeclared_outputs import undeclared_outputs_dir
from util.testing.visual_capture import VisualPage


@pytest.mark.asyncio
async def test_dashboard_visual(tmp_path: Path) -> None:
    dashboard_path = get_required_path(os.environ["GRAFANA_DASHBOARD_RLOCATION"])
    dashboard = json.loads(dashboard_path.read_text())
    fixture_path = os.environ["GRAFANA_FIXTURE_RLOCATION"]
    fixture = json.loads(get_required_path(fixture_path).read_text())
    validate_fixture(dashboard, fixture)
    with grafana_dashboard(
        tmp_path, dashboard_path, "", render_fixture=True, variables=fixture.get("variables", {})
    ) as base_url:
        async with async_playwright() as playwright:
            async with deterministic_browser_context(
                playwright, viewport={"width": 1440, "height": 900}, frozen_now_ms=FROZEN_NOW_MS, color_scheme="dark"
            ) as context:
                page = await context.new_page()
                seen = asyncio.Event()
                missing: list[str] = []
                await page.route(
                    "**/api/ds/query*", lambda route: replay_query(route, seen, fixture, dashboard, missing)
                )
                captures = tmp_path / "captures"
                view = VisualPage(page, output_dir=captures, title=f"{dashboard['title']} (recorded)")
                start = FROZEN_NOW_MS - 24 * 60 * 60 * 1000
                record_stage("loading dashboard with recorded query results")
                await page.goto(
                    f"{base_url}/d/{dashboard['uid']}/dashboard?orgId=1&kiosk=1&render=1&fullPageImage=true&from={start}&to={FROZEN_NOW_MS}",
                    wait_until="domcontentloaded",
                )
                await expect(page.locator(".react-grid-item").first).to_be_visible()
                await asyncio.wait_for(seen.wait(), timeout=20)
                record_stage("capturing full dashboard")
                await capture_dashboard(view)
                assert not missing, f"missing recorded datasource results: {missing}"
                assert await page.get_by_text("Data is missing a string field", exact=True).count() == 0
                errors = page.get_by_test_id("data-testid Panel status error")
                if await errors.count():
                    await errors.first.click()
                    (undeclared_outputs_dir() / "panel-error.txt").write_text(await page.locator("body").inner_text())
                    raise AssertionError("Grafana reported a panel error")
                output = undeclared_outputs_dir()
                for capture in captures.iterdir():
                    if capture.name != "visual-review.json":
                        shutil.copy2(capture, output / capture.name)
                shutil.copy2(captures / "visual-review.json", output / "visual-review.json")
                record_stage("captures complete")


if __name__ == "__main__":
    pytest_bazel.main()
