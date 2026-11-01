"""Isolated Grafana and Prometheus fixture for dashboard visual tests."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import docker
import httpx

from third_party.containers import grafana_visual, prometheus_visual
from util.oci import load_oci_image
from util.testing.undeclared_outputs import undeclared_outputs_dir

TEST_PASSWORD = "isolated-synthetic-grafana"


def _wait_ready(url: str) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=1, trust_env=False).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise AssertionError(f"service did not become ready: {url}")


@contextmanager
def grafana_dashboard(tmp_path: Path, dashboard_path: Path, samples: str) -> Iterator[str]:
    load_oci_image(grafana_visual.IMAGE)
    load_oci_image(prometheus_visual.IMAGE)
    client = docker.from_env()
    unique = uuid.uuid4().hex[:12]
    data = tmp_path / "data"
    data.mkdir()
    (data / "fixture.om").write_text(samples)
    mount = {str(data): {"bind": "/fixture", "mode": "rw"}}
    # Create a fixed-time TSDB rather than replaying scrape traffic during a screenshot.
    client.containers.run(
        prometheus_visual.IMAGE.tag,
        ["tsdb", "create-blocks-from", "openmetrics", "/fixture/fixture.om", "/fixture/tsdb"],
        entrypoint="/bin/promtool",
        volumes=mount,
        user="0",
        remove=True,
    )
    network = client.networks.create(f"grafana-visual-{unique}", internal=True)
    prometheus = None
    grafana = None
    try:
        prom_name = f"grafana-prometheus-{unique}"
        prometheus = client.containers.run(
            prometheus_visual.IMAGE.tag,
            [
                "--config.file=/etc/prometheus/prometheus.yml",
                "--storage.tsdb.path=/fixture/tsdb",
                "--storage.tsdb.retention.time=87600h",
            ],
            name=prom_name,
            user="0",
            network=network.name,
            volumes=mount,
            detach=True,
        )
        grafana = client.containers.run(
            grafana_visual.IMAGE.tag,
            name=f"grafana-visual-{unique}",
            network=network.name,
            environment={
                "GF_AUTH_ANONYMOUS_ENABLED": "true",
                "GF_AUTH_ANONYMOUS_ORG_ROLE": "Viewer",
                "GF_ANALYTICS_REPORTING_ENABLED": "false",
                "GF_ANALYTICS_CHECK_FOR_UPDATES": "false",
                "GF_PLUGINS_PREINSTALL": "",
                "GF_SECURITY_ADMIN_PASSWORD": TEST_PASSWORD,
                "GF_USERS_DEFAULT_THEME": "dark",
            },
            detach=True,
        )
        deadline = time.monotonic() + 20
        address = None
        while time.monotonic() < deadline:
            grafana.reload()
            if grafana.status in ("exited", "dead"):
                raise AssertionError(f"Grafana exited before joining the network: {grafana.attrs['State']}")
            address = grafana.attrs["NetworkSettings"].get("Networks", {}).get(network.name, {}).get("IPAddress")
            if address:
                break
            time.sleep(0.1)
        if not address:
            raise AssertionError(f"Grafana did not join its network: {grafana.attrs['State']}")
        url = f"http://{address}:3000"
        _wait_ready(f"{url}/api/health")
        with httpx.Client(base_url=url, timeout=15, auth=("admin", TEST_PASSWORD), trust_env=False) as http:
            response = http.post(
                "/api/datasources",
                json={
                    "name": "Mimir",
                    "uid": "mimir",
                    "type": "prometheus",
                    "access": "proxy",
                    "url": f"http://{prom_name}:9090",
                },
            )
            response.raise_for_status()
            dashboard = json.loads(dashboard_path.read_text())
            dashboard["id"] = None
            dashboard["version"] = 0
            dashboard = json.loads(json.dumps(dashboard).replace("${DS_MIMIR}", "Mimir"))
            response = http.post("/api/dashboards/db", json={"dashboard": dashboard, "overwrite": False})
            response.raise_for_status()
        yield url
    finally:
        output = undeclared_outputs_dir()
        for name, container in (("grafana", grafana), ("prometheus", prometheus)):
            if container is not None:
                (output / f"{name}.log").write_bytes(container.logs())
                container.reload()
                (output / f"{name}-state.json").write_text(json.dumps(container.attrs["State"], indent=2))
                (output / f"{name}-network.json").write_text(json.dumps(container.attrs["NetworkSettings"], indent=2))
                container.remove(force=True)
        network.remove()
        client.close()
