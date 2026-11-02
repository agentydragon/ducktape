"""Evaluate renderer output and error handling with real Prometheus queries."""

from __future__ import annotations

import json
import os
import struct
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlencode

import httpx
import pytest
import pytest_bazel

from cluster.cdk8s.monitoring.dashboards import piraeus_metrics
from grafana.visual_tests.renderer_spike_fixtures import load_images
from grafana.visual_tests.visual_harness import grafana_dashboard, record_stage
from third_party.containers import grafana_image_renderer_spike, grafana_visual, prometheus_visual, ryuk
from util.bazel.runfiles import get_required_path
from util.testing.undeclared_outputs import undeclared_outputs_dir

pytest_plugins = ["grafana.visual_tests.renderer_spike_fixtures"]


@pytest.fixture
def scenario() -> str:
    return os.environ["RENDER_SCENARIO"]


@pytest.fixture
def dashboard_path(tmp_path: Path, scenario: str) -> Path:
    dashboard_path = get_required_path(os.environ["GRAFANA_DASHBOARD_RLOCATION"])
    if scenario in {"invalid-query", "filtered"}:
        dashboard = json.loads(dashboard_path.read_text())
        panel_id = 26 if scenario == "invalid-query" else 27
        panel = next(panel for panel in dashboard["panels"] if panel.get("id") == panel_id)
        if scenario == "invalid-query":
            panel["targets"][0]["expr"] = "this is not valid promql {{{"
        dashboard["panels"] = [panel]
        dashboard_path = tmp_path / "single-panel-dashboard.json"
        dashboard_path.write_text(json.dumps(dashboard))
    return dashboard_path


@pytest.fixture
def grafana_url(tmp_path: Path, dashboard_path: Path) -> Iterator[str]:
    record_stage("loading renderer and service images")
    load_images(grafana_image_renderer_spike.IMAGE, grafana_visual.IMAGE, prometheus_visual.IMAGE, ryuk.IMAGE)
    record_stage("renderer and service images loaded")
    with grafana_dashboard(tmp_path, dashboard_path, piraeus_metrics.samples()) as url:
        yield url


def test_query_backed_image_renderer(grafana_url: str, renderer_url: str, scenario: str) -> None:
    node = "storage-a" if scenario == "filtered" else "All"
    target = (
        f"{grafana_url}/d/piraeus-storage/piraeus-lvm-storage?orgId=1&kiosk=1&render=1&fullPageImage=true"
        f"&from={piraeus_metrics.START * 1000}&to={piraeus_metrics.END * 1000}&var-node={node}"
    )
    params = {"url": target, "width": 1440, "height": -1, "encoding": "png", "timezone": "UTC"}
    output = undeclared_outputs_dir()
    record_stage("rendering " + scenario)
    response = httpx.get(
        renderer_url + "/render?" + urlencode(params), headers={"X-Auth-Token": "spike"}, timeout=45, trust_env=False
    )
    (output / f"{scenario}-status.txt").write_text(f"HTTP {response.status_code}\n")
    if response.status_code == 200:
        assert response.content[:8] == b"\x89PNG\r\n\x1a\n"
        width, height = struct.unpack(">II", response.content[16:24])
        assert width == 1440, width
        assert height > (1200 if scenario == "valid" else 200), height
        (output / f"{scenario}.png").write_bytes(response.content)
    else:
        (output / f"{scenario}-error.txt").write_text(response.text[:5000])
    if scenario != "invalid-query":
        response.raise_for_status()


if __name__ == "__main__":
    pytest_bazel.main()
