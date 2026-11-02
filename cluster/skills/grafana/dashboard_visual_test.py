"""Render a dashboard with deterministic, synthetic datasource responses.

This scenario tests dashboard appearance, not query execution. A dashboard can
provide a custom scenario with real isolated datasources when query behavior matters.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from pathlib import Path

import pytest
import pytest_bazel
from playwright.async_api import Route, async_playwright, expect

from cluster.skills.grafana.visual_browser import capture_dashboard
from cluster.skills.grafana.visual_harness import grafana_dashboard, record_stage
from util.bazel.runfiles import get_required_path
from util.testing.frontend_visual import FROZEN_NOW_MS, deterministic_browser_context
from util.testing.undeclared_outputs import undeclared_outputs_dir
from util.testing.visual_capture import VisualPage


async def _synthetic_query(route: Route, seen: asyncio.Event, fixture: dict, log_panels: set[str]) -> None:
    payload = route.request.post_data_json
    assert payload is not None
    with (undeclared_outputs_dir() / "synthetic-queries.jsonl").open("a") as log:
        log.write(json.dumps(payload) + "\n")
    start = FROZEN_NOW_MS - 24 * 60 * 60 * 1000
    times = [start + i * 60 * 60 * 1000 for i in range(25)]
    results = {}
    for query in payload["queries"]:
        ref = query["refId"]
        request_id = query.get("requestId", "")
        panel_id = request_id.removesuffix(ref)
        panel = fixture.get(panel_id, {})
        if panel_id in log_panels:
            results[ref] = {
                "refId": ref,
                "frames": [
                    {
                        "schema": {
                            "refId": ref,
                            "fields": [
                                {"name": "Time", "type": "time", "typeInfo": {"frame": "time.Time"}},
                                {"name": "Line", "type": "string"},
                            ],
                        },
                        "data": {
                            "values": [
                                [FROZEN_NOW_MS - 120000, FROZEN_NOW_MS - 60000],
                                ["synthetic: interface became ready", "synthetic: link state changed"],
                            ]
                        },
                    }
                ],
            }
            continue
        if "table" in panel:
            columns = panel["table"]
            results[ref] = {
                "refId": ref,
                "frames": [
                    {
                        "schema": {
                            "refId": ref,
                            "fields": [{"name": col["name"], "type": col["type"]} for col in columns],
                        },
                        "data": {"values": [col["values"] for col in columns]},
                    }
                ],
            }
            continue
        if query.get("format") == "table":
            # Prometheus returns one instant-value frame per label set; its
            # frontend converts those frames to table rows before joins.
            labels = {
                "node": ["storage-a", "storage-b"],
                "namespace": ["monitoring", "seaweedfs"],
                "pod": ["exporter-a", "server-b"],
                "pod_key": ["monitoring/exporter-a", "seaweedfs/server-b"],
                "part": ["Pod working set", "Available"],
                "category": ["databases", "other"],
                "device": ["sda", "sdb"],
                "storage_pool": ["hdd", "ssd"],
                "persistentvolumeclaim": ["data-a", "data-b"],
            }
            expression = query.get("expr", "")
            selected = {key: values for key, values in labels.items() if re.search(r"\b" + key + r"\b", expression)}
            frames = [
                {
                    "schema": {
                        "refId": ref,
                        "meta": {"type": "numeric-multi", "typeVersion": [0, 1], "custom": {"resultType": "vector"}},
                        "fields": [
                            {"name": "Time", "type": "time", "typeInfo": {"frame": "time.Time"}},
                            {
                                "name": "Value",
                                "type": "number",
                                "typeInfo": {"frame": "float64"},
                                "labels": {key: values[index] for key, values in selected.items()},
                            },
                        ],
                    },
                    "data": {"values": [[FROZEN_NOW_MS], [25 + 15 * index]]},
                }
                for index in range(2)
            ]
            results[ref] = {"refId": ref, "frames": frames}
            continue
        frames = []
        for index, node in enumerate(panel.get("series", ["storage-a", "storage-b"])):
            frames.append(
                {
                    "schema": {
                        "refId": ref,
                        "fields": [
                            {"name": "Time", "type": "time", "typeInfo": {"frame": "time.Time"}},
                            {"name": "Value", "type": "number", "labels": {"node": node}},
                        ],
                    },
                    "data": {"values": [times, [20 + index * 15 + (i % 7) * 2 for i in range(25)]]},
                }
            )
        results[ref] = {"frames": frames, "refId": ref}
    await route.fulfill(json={"results": results})
    seen.set()


@pytest.mark.asyncio
async def test_dashboard_visual(tmp_path: Path) -> None:
    dashboard_path = get_required_path(os.environ["GRAFANA_DASHBOARD_RLOCATION"])
    dashboard = json.loads(dashboard_path.read_text())
    log_panels = {str(panel["id"]) for panel in dashboard.get("panels", []) if panel.get("type") == "logs"}
    fixture_path = os.environ.get("GRAFANA_FIXTURE_RLOCATION")
    fixture = json.loads(get_required_path(fixture_path).read_text()) if fixture_path else {}
    samples = "# TYPE fixture gauge\nfixture 1 1704067200\n# EOF\n"
    with grafana_dashboard(tmp_path, dashboard_path, samples, render_fixture=True) as base_url:
        async with async_playwright() as playwright:
            async with deterministic_browser_context(
                playwright, viewport={"width": 1440, "height": 900}, frozen_now_ms=FROZEN_NOW_MS, color_scheme="dark"
            ) as context:
                page = await context.new_page()
                seen = asyncio.Event()
                await page.route("**/api/ds/query*", lambda route: _synthetic_query(route, seen, fixture, log_panels))
                captures = tmp_path / "captures"
                view = VisualPage(page, output_dir=captures, title=f"{dashboard['title']} (synthetic)")
                start = FROZEN_NOW_MS - 24 * 60 * 60 * 1000
                record_stage("loading dashboard with synthetic query results")
                await page.goto(
                    f"{base_url}/d/{dashboard['uid']}/dashboard?orgId=1&kiosk=1&render=1&fullPageImage=true&from={start}&to={FROZEN_NOW_MS}",
                    wait_until="domcontentloaded",
                )
                await expect(page.locator(".react-grid-item").first).to_be_visible()
                await asyncio.wait_for(seen.wait(), timeout=20)
                record_stage("capturing full dashboard")
                await capture_dashboard(view)
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
