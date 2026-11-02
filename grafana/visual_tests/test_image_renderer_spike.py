"""Compare the upstream Grafana image renderer with the recorded-data visual harness."""

from __future__ import annotations

import json
import os
import struct
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
import pytest
import pytest_bazel
from docker.models.networks import Network

from grafana.visual_tests.recorded import recorded_result, validate_fixture
from grafana.visual_tests.renderer_spike_fixtures import load_images
from grafana.visual_tests.visual_harness import grafana_dashboard, record_stage
from third_party.containers import grafana_image_renderer_spike, grafana_visual, ryuk
from util.bazel.runfiles import get_required_path
from util.testing.frontend_visual import FROZEN_NOW_MS
from util.testing.undeclared_outputs import undeclared_outputs_dir

pytest_plugins = ["grafana.visual_tests.renderer_spike_fixtures"]


@dataclass
class Recording:
    dashboard: dict[str, Any]
    fixture: dict[str, Any]
    dashboard_path: Path
    seen: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    injected: list[str] = field(default_factory=list)


@pytest.fixture
def recording(tmp_path: Path) -> Recording:
    dashboard_path = get_required_path(os.environ["GRAFANA_DASHBOARD_RLOCATION"])
    dashboard = json.loads(dashboard_path.read_text())
    fixture = json.loads(get_required_path(os.environ["GRAFANA_FIXTURE_RLOCATION"]).read_text())
    validate_fixture(dashboard, fixture)
    if os.environ.get("INJECT_QUERY_ERROR") == "1":
        dashboard["panels"] = [panel for panel in dashboard["panels"] if panel.get("id") == 26]
        dashboard_path = tmp_path / "broken-panel-dashboard.json"
        dashboard_path.write_text(json.dumps(dashboard))
    return Recording(dashboard=dashboard, fixture=fixture, dashboard_path=dashboard_path)


@pytest.fixture
def grafana_url(tmp_path: Path, recording: Recording) -> Iterator[str]:
    record_stage("loading renderer and service images")
    load_images(grafana_image_renderer_spike.IMAGE, grafana_visual.IMAGE, ryuk.IMAGE)
    record_stage("renderer and service images loaded")
    with grafana_dashboard(
        tmp_path, recording.dashboard_path, "", render_fixture=True, variables=recording.fixture.get("variables", {})
    ) as url:
        yield url


@pytest.fixture
def recorded_proxy_server(recording: Recording, grafana_url: str) -> Iterator[ThreadingHTTPServer]:
    class Proxy(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def do_POST(self) -> None:
            if self.path.startswith("/api/ds/query"):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if os.environ.get("INJECT_QUERY_ERROR") == "1" and any(
                    q.get("requestId") == "26A" for q in body["queries"]
                ):
                    recording.injected.append("26A")
                    self.reply(400, b'{"message":"injected query failure"}', "application/json")
                    return
                try:
                    results = {}
                    for query in body["queries"]:
                        result = recorded_result(query, recording.fixture, recording.dashboard)
                        results[query["refId"]] = result
                        recording.seen.append(query.get("requestId", ""))
                    self.reply(200, json.dumps({"results": results}).encode(), "application/json")
                except KeyError as error:
                    recording.missing.append(str(error))
                    self.reply(400, str(error).encode(), "text/plain")
                return
            self.forward()

        def do_GET(self) -> None:
            self.forward()

        def reply(self, status: int, data: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def forward(self) -> None:
            headers = {
                k: v
                for k, v in self.headers.items()
                if k.lower() not in {"host", "accept-encoding", "connection", "content-length"}
            }
            headers["Accept-Encoding"] = "identity"
            size = int(self.headers.get("Content-Length", "0"))
            data = self.rfile.read(size) if size else None
            with httpx.Client(trust_env=False, timeout=30, follow_redirects=False) as client:
                response = client.request(self.command, grafana_url + self.path, headers=headers, content=data)
            self.send_response(response.status_code)
            for key, value in response.headers.items():
                if key.lower() not in {"content-length", "content-encoding", "transfer-encoding", "connection"}:
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)

    with ThreadingHTTPServer(("0.0.0.0", 0), Proxy) as proxy:
        yield proxy


@pytest.fixture
def recorded_proxy(recorded_proxy_server: ThreadingHTTPServer) -> Iterator[ThreadingHTTPServer]:
    thread = threading.Thread(target=recorded_proxy_server.serve_forever, daemon=True)
    thread.start()
    try:
        yield recorded_proxy_server
    finally:
        recorded_proxy_server.shutdown()
        thread.join()


def test_image_renderer(
    recording: Recording, recorded_proxy: ThreadingHTTPServer, grafana_network: Network, renderer_url: str
) -> None:
    gateway = grafana_network.attrs["IPAM"]["Config"][0]["Gateway"]
    start = FROZEN_NOW_MS - 24 * 60 * 60 * 1000
    target = f"http://{gateway}:{recorded_proxy.server_port}/d/{recording.dashboard['uid']}/dashboard?orgId=1&kiosk=1&render=1&fullPageImage=true&from={start}&to={FROZEN_NOW_MS}"
    record_stage("rendering full dashboard using upstream renderer")
    params = {"url": target, "width": 1440, "height": -1, "encoding": "png", "timezone": "UTC"}
    response = httpx.get(
        renderer_url + "/render?" + urlencode(params), headers={"X-Auth-Token": "spike"}, timeout=45, trust_env=False
    )
    if os.environ.get("INJECT_QUERY_ERROR") == "1":
        assert recording.injected, "the deliberately failing panel was not queried"
        (undeclared_outputs_dir() / "error-response.txt").write_text(f"HTTP {response.status_code}\n")
        if response.status_code != 200:
            return
    if response.status_code != 200:
        raise AssertionError(f"renderer returned {response.status_code}: {response.text[:500]}")
    png = response.content
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", png[16:24])
    assert width == 1440, width
    assert height > 200, height
    if os.environ.get("INJECT_QUERY_ERROR") != "1":
        assert height > 1200, height
        assert recording.seen, "renderer did not request any recorded dashboard queries"
    assert not recording.missing, recording.missing
    output = undeclared_outputs_dir()
    (output / "renderer-dashboard.png").write_bytes(png)
    (output / "query-coverage.json").write_text(
        json.dumps({"seen": recording.seen, "missing": recording.missing, "width": width, "height": height}, indent=2)
    )
    record_stage("upstream renderer screenshot complete")


if __name__ == "__main__":
    pytest_bazel.main()
