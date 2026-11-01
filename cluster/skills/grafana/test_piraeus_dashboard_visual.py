"""Render the real Piraeus dashboard against synthetic Prometheus metrics."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
import pytest_bazel
from playwright.async_api import Response, async_playwright, expect

from cluster.skills.grafana import piraeus_metrics
from cluster.skills.grafana.visual_harness import grafana_dashboard
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
                view = VisualPage(page, output_dir=undeclared_outputs_dir(), title="Piraeus Grafana dashboard")
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
                await page.goto(url, wait_until="networkidle")
                await expect(
                    page.get_by_text("Piraeus thin-pool accounting by storage node", exact=True)
                ).to_be_visible()
                await view.capture("overview", animations="disabled")
                graph = page.get_by_text("Physical pool usage", exact=True).first
                await page.get_by_text("Piraeus thin-pool accounting by storage node", exact=True).hover()
                await page.mouse.wheel(0, 900)
                await expect(graph).to_be_visible()
                await page.wait_for_load_state("networkidle")
                await view.capture("pool-graphs", animations="disabled")
                assert await page.get_by_text("No data", exact=True).count() == 0
                await page.set_viewport_size({"width": 1024, "height": 768})
                await page.goto(url.replace("var-node=All", "var-node=storage-a"), wait_until="networkidle")
                await expect(page.get_by_text("4 TiB", exact=True).first).to_be_visible()
                await view.capture("single-node-narrow", animations="disabled")
                await asyncio.gather(*checks)
                assert not query_errors, query_errors


if __name__ == "__main__":
    pytest_bazel.main()
