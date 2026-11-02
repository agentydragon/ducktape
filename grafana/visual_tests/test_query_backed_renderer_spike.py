"""Evaluate renderer output and error handling with real Prometheus queries."""

from __future__ import annotations

import json
import os
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlencode

import docker
import httpx
import pytest_bazel

from cluster.cdk8s.monitoring.dashboards import piraeus_metrics
from grafana.visual_tests.visual_harness import grafana_dashboard, record_stage
from third_party.containers import grafana_image_renderer_spike, grafana_visual, prometheus_visual, ryuk
from util.bazel.runfiles import get_required_path
from util.oci import load_oci_image
from util.testing.undeclared_outputs import undeclared_outputs_dir


def test_query_backed_image_renderer(tmp_path: Path) -> None:
    dashboard_path = get_required_path(os.environ["GRAFANA_DASHBOARD_RLOCATION"])
    scenario = os.environ["RENDER_SCENARIO"]
    if scenario in {"invalid-query", "filtered"}:
        dashboard = json.loads(dashboard_path.read_text())
        panel_id = 26 if scenario == "invalid-query" else 27
        panel = next(panel for panel in dashboard["panels"] if panel.get("id") == panel_id)
        if scenario == "invalid-query":
            panel["targets"][0]["expr"] = "this is not valid promql {{{"
        dashboard["panels"] = [panel]
        dashboard_path = tmp_path / "single-panel-dashboard.json"
        dashboard_path.write_text(json.dumps(dashboard))
    node = "storage-a" if scenario == "filtered" else "All"
    record_stage("loading renderer and service images")
    with ThreadPoolExecutor(max_workers=4) as executor:
        images = [grafana_image_renderer_spike.IMAGE, grafana_visual.IMAGE, prometheus_visual.IMAGE, ryuk.IMAGE]
        for loaded in executor.map(load_oci_image, images):
            assert loaded
    record_stage("renderer and service images loaded")
    with grafana_dashboard(tmp_path, dashboard_path, piraeus_metrics.samples()) as grafana_url:
        client = docker.from_env()
        grafana_ip = grafana_url.split("//")[1].split(":")[0]
        grafana = next(
            c
            for c in client.containers.list()
            if any(n.get("IPAddress") == grafana_ip for n in c.attrs["NetworkSettings"]["Networks"].values())
        )
        network = next(iter(grafana.attrs["NetworkSettings"]["Networks"]))
        renderer = None
        log_thread = None
        try:
            record_stage("starting upstream image renderer")
            renderer = client.containers.run(
                grafana_image_renderer_spike.IMAGE.tag,
                detach=True,
                network=network,
                environment={"AUTH_TOKEN": "spike", "BROWSER_FLAGS": "--no-sandbox"},
            )

            def collect_logs() -> None:
                assert renderer is not None
                with (undeclared_outputs_dir() / "renderer.log").open("wb", buffering=0) as log:
                    for chunk in renderer.logs(stream=True, follow=True):
                        log.write(chunk)

            log_thread = threading.Thread(target=collect_logs, daemon=True)
            log_thread.start()
            renderer.reload()
            renderer_ip = renderer.attrs["NetworkSettings"]["Networks"][network]["IPAddress"]
            base = f"http://{renderer_ip}:8081"
            for _ in range(100):
                try:
                    if httpx.get(base + "/healthz", timeout=0.2, trust_env=False).status_code == 200:
                        break
                except httpx.HTTPError:
                    time.sleep(0.1)
            else:
                raise AssertionError("image renderer health endpoint did not respond")
            target = (
                f"{grafana_url}/d/piraeus-storage/piraeus-lvm-storage?orgId=1&kiosk=1&render=1&fullPageImage=true"
                f"&from={piraeus_metrics.START * 1000}&to={piraeus_metrics.END * 1000}&var-node={node}"
            )
            params = {"url": target, "width": 1440, "height": -1, "encoding": "png", "timezone": "UTC"}
            render_url = base + "/render?" + urlencode(params)
            output = undeclared_outputs_dir()

            def capture(name: str) -> None:
                record_stage("rendering " + name)
                response = httpx.get(render_url, headers={"X-Auth-Token": "spike"}, timeout=45, trust_env=False)
                (output / f"{name}-status.txt").write_text(f"HTTP {response.status_code}\n")
                if response.status_code == 200:
                    assert response.content[:8] == b"\x89PNG\r\n\x1a\n"
                    width, height = struct.unpack(">II", response.content[16:24])
                    assert width == 1440, width
                    assert height > (1200 if scenario == "valid" else 200), height
                    (output / f"{name}.png").write_bytes(response.content)
                else:
                    (output / f"{name}-error.txt").write_text(response.text[:5000])
                if scenario != "invalid-query":
                    response.raise_for_status()

            capture(scenario)
        finally:
            if renderer:
                renderer.remove(force=True)
                if log_thread:
                    log_thread.join(timeout=2)
            client.close()


if __name__ == "__main__":
    pytest_bazel.main()
