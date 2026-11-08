"""Render the real Piraeus dashboard against synthetic Prometheus metrics."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
import pytest_bazel
from playwright.async_api import Response, async_playwright, expect
from python.runfiles import Runfiles

from cluster.cdk8s.monitoring.dashboards import piraeus_metrics
from devinfra.testing.output_writer import BoundTestOutputs
from grafana.visual_tests.visual_browser import capture_dashboard
from grafana.visual_tests.visual_harness import grafana_dashboard, record_stage
from util.testing.frontend_visual.capture import VisualPage
from util.testing.frontend_visual.context import FROZEN_NOW_MS, deterministic_browser_context

pytest_plugins = ("devinfra.testing.pytest_outputs",)


@pytest.mark.asyncio
async def test_piraeus_dashboard_visual(tmp_path: Path, test_outputs: BoundTestOutputs) -> None:
    with grafana_dashboard(
        tmp_path,
        (Runfiles.CreateOrRaise().root() / os.environ["GRAFANA_DASHBOARD_RLOCATION"]),
        piraeus_metrics.samples(),
    ) as base_url:
        async with async_playwright() as playwright:
            async with deterministic_browser_context(
                playwright, viewport={"width": 1440, "height": 900}, frozen_now_ms=FROZEN_NOW_MS, color_scheme="dark"
            ) as context:
                page = await context.new_page()
                with VisualPage(page, outputs=test_outputs, title="Piraeus Grafana dashboard") as view:
                    query_errors: list[str] = []
                    checks: list[asyncio.Task[None]] = []

                    async def check_query(response: Response) -> None:
                        if "/api/ds/query" not in response.url:
                            return
                        if response.status >= 400:
                            query_errors.append(f"datasource HTTP {response.status}: {response.url}")
                            return
                        body = await response.json()
                        query_errors.extend(
                            str(result["error"]) for result in body.get("results", {}).values() if result.get("error")
                        )

                    page.on("response", lambda response: checks.append(asyncio.create_task(check_query(response))))
                    url = f"{base_url}/d/piraeus-storage/piraeus-lvm-storage?orgId=1&kiosk=1&render=1&fullPageImage=true&from={piraeus_metrics.START * 1000}&to={piraeus_metrics.END * 1000}&var-node=All"
                    record_stage("loading overview")
                    await page.goto(url, wait_until="domcontentloaded")
                    await expect(
                        page.get_by_text("Piraeus thin-pool accounting by storage node", exact=True)
                    ).to_be_visible()
                    capacity = page.get_by_role("region", name="Physical pool capacity")
                    await expect(capacity.get_by_text("6 TiB", exact=True)).to_be_visible()
                    await expect(capacity.get_by_text("3 TiB", exact=True)).to_be_visible()
                    record_stage("capturing full dashboard")
                    await capture_dashboard(view)
                    assert await page.get_by_text("No data", exact=True).count() == 0
                    await asyncio.gather(*checks)
                    assert not query_errors, query_errors
                    record_stage("checking tier selection on mixed-pool node")
                    for tier, expected_capacity in (("hdd", "2 TiB"), ("ssd", "1 TiB")):
                        record_stage(f"checking {tier} on mixed-pool node")
                        tier_page = await context.new_page()
                        await tier_page.goto(
                            url.replace("var-node=All", f"var-node=storage-mixed&var-storage_tier={tier}"),
                            wait_until="domcontentloaded",
                        )
                        tier_capacity = tier_page.get_by_role("region", name="Physical pool capacity")
                        await expect(tier_capacity.get_by_text(expected_capacity, exact=True)).to_be_visible()
                        await tier_page.close()
                    record_stage("captures complete")


if __name__ == "__main__":
    pytest_bazel.main()
