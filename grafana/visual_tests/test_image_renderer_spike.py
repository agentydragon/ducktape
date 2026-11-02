"""Compare the upstream Grafana image renderer with the recorded-data visual harness."""

from __future__ import annotations

import json
import os
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode

import docker
import httpx
import pytest_bazel

from grafana.visual_tests.recorded import recorded_result, validate_fixture
from grafana.visual_tests.visual_harness import grafana_dashboard, record_stage
from third_party.containers import grafana_image_renderer_spike
from util.bazel.runfiles import get_required_path
from util.oci import load_oci_image
from util.testing.frontend_visual import FROZEN_NOW_MS
from util.testing.undeclared_outputs import undeclared_outputs_dir


def test_image_renderer(tmp_path: Path) -> None:
    dashboard_path = get_required_path(os.environ["GRAFANA_DASHBOARD_RLOCATION"])
    dashboard = json.loads(dashboard_path.read_text())
    fixture = json.loads(get_required_path(os.environ["GRAFANA_FIXTURE_RLOCATION"]).read_text())
    validate_fixture(dashboard, fixture)
    load_oci_image(grafana_image_renderer_spike.IMAGE)
    seen: list[str] = []
    missing: list[str] = []

    with grafana_dashboard(
        tmp_path, dashboard_path, "", render_fixture=True, variables=fixture.get("variables", {})
    ) as grafana_url:

        class Proxy(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def do_POST(self) -> None:
                if self.path.startswith("/api/ds/query"):
                    body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    try:
                        results = {}
                        for query in body["queries"]:
                            result = recorded_result(query, fixture, dashboard)
                            results[query["refId"]] = result
                            seen.append(query.get("requestId", ""))
                        self.reply(200, json.dumps({"results": results}).encode(), "application/json")
                    except KeyError as error:
                        missing.append(str(error))
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

        proxy = ThreadingHTTPServer(("0.0.0.0", 0), Proxy)
        thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        client = docker.from_env()
        grafana = next(
            c
            for c in client.containers.list()
            if any(
                n.get("IPAddress") == grafana_url.split("//")[1].split(":")[0]
                for n in c.attrs["NetworkSettings"]["Networks"].values()
            )
        )
        network_name = next(iter(grafana.attrs["NetworkSettings"]["Networks"]))
        network = client.networks.get(network_name)
        gateway = network.attrs["IPAM"]["Config"][0]["Gateway"]
        renderer = None
        try:
            record_stage("starting upstream image renderer")
            renderer = client.containers.run(
                grafana_image_renderer_spike.IMAGE.tag,
                detach=True,
                network=network_name,
                environment={"AUTH_TOKEN": "spike", "BROWSER_FLAGS": "--no-sandbox"},
            )
            renderer.reload()
            renderer_ip = renderer.attrs["NetworkSettings"]["Networks"][network_name]["IPAddress"]
            base = f"http://{renderer_ip}:8081"
            for _ in range(100):
                try:
                    if httpx.get(base + "/healthz", timeout=0.2, trust_env=False).status_code == 200:
                        break
                except httpx.HTTPError:
                    threading.Event().wait(0.1)
            else:
                raise AssertionError("image renderer health endpoint did not respond")
            start = FROZEN_NOW_MS - 24 * 60 * 60 * 1000
            target = f"http://{gateway}:{proxy.server_port}/d/{dashboard['uid']}/dashboard?orgId=1&kiosk=1&render=1&fullPageImage=true&from={start}&to={FROZEN_NOW_MS}"
            record_stage("rendering full dashboard using upstream renderer")
            params = {"url": target, "width": 1440, "height": -1, "encoding": "png", "timezone": "UTC"}
            response = httpx.get(
                base + "/render?" + urlencode(params), headers={"X-Auth-Token": "spike"}, timeout=45, trust_env=False
            )
            if response.status_code != 200:
                raise AssertionError(f"renderer returned {response.status_code}: {response.text[:500]}")
            png = response.content
            assert png[:8] == b"\x89PNG\r\n\x1a\n"
            width, height = struct.unpack(">II", png[16:24])
            assert width == 1440, width
            assert height > 1200, height
            assert seen, "renderer did not request any recorded dashboard queries"
            assert not missing, missing
            output = undeclared_outputs_dir()
            (output / "renderer-dashboard.png").write_bytes(png)
            (output / "query-coverage.json").write_text(
                json.dumps({"seen": seen, "missing": missing, "width": width, "height": height}, indent=2)
            )
            record_stage("upstream renderer screenshot complete")
        finally:
            if renderer:
                (undeclared_outputs_dir() / "renderer.log").write_bytes(renderer.logs())
                renderer.remove(force=True)
            client.close()
            proxy.shutdown()
            proxy.server_close()
            thread.join()


if __name__ == "__main__":
    pytest_bazel.main()
