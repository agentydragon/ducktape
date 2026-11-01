"""Render the real Piraeus dashboard against synthetic Prometheus metrics."""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import pytest
import pytest_bazel
from playwright.async_api import Response, async_playwright, expect

from cluster.cdk8s.monitoring.dashboards import piraeus_metrics
from cluster.skills.grafana.visual_harness import grafana_dashboard, record_stage
from util.bazel.runfiles import get_required_path
from util.testing.frontend_visual import FROZEN_NOW_MS, deterministic_browser_context
from util.testing.undeclared_outputs import undeclared_outputs_dir
from util.testing.visual_capture import VisualPage


@pytest.mark.asyncio
async def test_piraeus_dashboard_visual(tmp_path: Path) -> None:
    with grafana_dashboard(
        tmp_path, get_required_path(os.environ["GRAFANA_DASHBOARD_RLOCATION"]), piraeus_metrics.samples()
    ) as base_url:
        async with async_playwright() as playwright:
            async with deterministic_browser_context(
                playwright, viewport={"width": 1440, "height": 900}, frozen_now_ms=FROZEN_NOW_MS, color_scheme="dark"
            ) as context:
                page = await context.new_page()
                captures = tmp_path / "captures"
                view = VisualPage(page, output_dir=captures, title="Piraeus Grafana dashboard")
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
                url = f"{base_url}/d/piraeus-storage/piraeus-lvm-storage?orgId=1&from={piraeus_metrics.START * 1000}&to={piraeus_metrics.END * 1000}&var-node=All"
                record_stage("loading overview")
                await page.goto(url, wait_until="domcontentloaded")
                await expect(
                    page.get_by_text("Piraeus thin-pool accounting by storage node", exact=True)
                ).to_be_visible()
                await expect(page.get_by_text("9 TiB", exact=True).first).to_be_visible()
                await view.capture("overview", animations="disabled")
                record_stage("loading pool graphs")
                graph = page.get_by_text("Physical pool usage", exact=True).first
                await page.get_by_text("Piraeus thin-pool accounting by storage node", exact=True).hover()
                await page.mouse.wheel(0, 900)
                await expect(graph).to_be_visible()
                await view.capture("pool-graphs", animations="disabled")
                assert await page.get_by_text("No data", exact=True).count() == 0
                await page.set_viewport_size({"width": 1024, "height": 768})
                record_stage("loading narrow single-node view")
                await page.goto(url.replace("var-node=All", "var-node=storage-a"), wait_until="domcontentloaded")
                record_stage("narrow page loaded")
                await expect(page.get_by_text("4 TiB", exact=True).first).to_be_visible()
                record_stage("narrow data visible")
                await view.capture("single-node-narrow", animations="disabled")
                record_stage("narrow capture complete")
                await asyncio.gather(*checks)
                assert not query_errors, query_errors
                output = undeclared_outputs_dir()
                for capture in captures.iterdir():
                    if capture.name != "visual-review.json":
                        shutil.copy2(capture, output / capture.name)
                shutil.copy2(captures / "visual-review.json", output / "visual-review.json")
                record_stage("captures complete")


if __name__ == "__main__":
    pytest_bazel.main()
