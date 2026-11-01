"""Render the real Piraeus dashboard against a synthetic Prometheus TSDB."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import docker
import httpx
import pytest
import pytest_bazel
from playwright.async_api import Response, async_playwright, expect

from third_party.containers import grafana_visual, prometheus_visual
from util.bazel.runfiles import get_required_path, own_repo_rlocation
from util.oci import load_oci_image
from util.testing.frontend_visual import FROZEN_NOW_MS, deterministic_browser_context
from util.testing.undeclared_outputs import undeclared_outputs_dir
from util.testing.visual_capture import VisualPage

DASHBOARD = "cluster/cdk8s/monitoring/dashboards/piraeus-storage.json"
END = FROZEN_NOW_MS // 1000
START = END - 24 * 60 * 60
TEST_PASSWORD = "isolated-synthetic-grafana"


def _samples() -> str:
    # Representative nodes and pools, including a node with both media.
    pools = [
        ("storage-a", "hdd", 4 * 2**40, 0.80),
        ("storage-b", "ssd", 2 * 2**40, 0.45),
        ("storage-mixed", "hdd", 2 * 2**40, 0.65),
        ("storage-mixed", "ssd", 1 * 2**40, 0.35),
    ]
    metrics: dict[str, list[tuple[dict[str, str], float]]] = {}

    def add(name: str, labels: dict[str, str], value: float) -> None:
        metrics.setdefault(name, []).append((labels, value))

    for i, (node, pool, capacity, used) in enumerate(pools):
        labels = {"node": node, "storage_pool": pool}
        add("disk_usage:pool_capacity_bytes", labels, capacity)
        add("disk_usage:pool_used_bytes", labels, capacity * used)
        add("disk_usage:pool_free_bytes", labels, capacity * (1 - used))
        add("disk_usage:pool_committed_bytes", labels, capacity * (used + 0.10))
        copy_labels = labels | {
            "phase": "Bound",
            "resource": f"volume-{i}",
            "persistentvolume": f"pv-{i}",
            "namespace": "example",
            "persistentvolumeclaim": f"data-{i}",
        }
        add("disk_usage:volume_allocated_bytes", copy_labels, capacity * used / 2)
        add("disk_usage:volume_capacity_bytes", copy_labels, capacity * 0.55)
        add(
            "disk_usage:pvc_used_bytes",
            {"node": node, "namespace": "example", "persistentvolumeclaim": f"data-{i}"},
            capacity * used / 3,
        )
    for node, tier in (("storage-a", "hdd"), ("storage-b", "ssd"), ("storage-mixed", "hdd")):
        add("kube_node_labels", {"node": node, "label_storage_allegedly_works_tier": tier}, 1)

    lines: list[str] = []
    for name, series in metrics.items():
        lines.append(f"# TYPE {name} gauge")
        for labels, value in series:
            encoded = ",".join(f'{key}="{item}"' for key, item in labels.items())
            lines.extend(f"{name}{{{encoded}}} {value} {timestamp}" for timestamp in range(START, END + 1, 300))
    return "\n".join([*lines, "# EOF", ""])


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
def _grafana(tmp_path: Path) -> Iterator[str]:
    load_oci_image(grafana_visual.IMAGE)
    load_oci_image(prometheus_visual.IMAGE)
    client = docker.from_env()
    unique = uuid.uuid4().hex[:12]
    data = tmp_path / "data"
    data.mkdir()
    (data / "fixture.om").write_text(_samples())
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
            dashboard = json.loads(get_required_path(own_repo_rlocation(DASHBOARD)).read_text())
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


@pytest.mark.asyncio
async def test_piraeus_dashboard_visual(tmp_path: Path) -> None:
    with _grafana(tmp_path) as base_url:
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
                url = f"{base_url}/d/piraeus-storage/piraeus-lvm-storage?orgId=1&from={START * 1000}&to={END * 1000}&var-node=All"
                await page.goto(url, wait_until="networkidle")
                await expect(
                    page.get_by_text("Piraeus thin-pool accounting by storage node", exact=True)
                ).to_be_visible()
                await view.capture("overview", animations="disabled")
                graph = page.get_by_text("Physical pool usage", exact=True).first
                await graph.scroll_into_view_if_needed()
                await expect(graph).to_be_visible()
                await page.wait_for_load_state("networkidle")
                await view.capture("pool-graphs", animations="disabled")
                assert await page.get_by_text("No data", exact=True).count() == 0
                await page.set_viewport_size({"width": 1024, "height": 768})
                await page.goto(url.replace("var-node=All", "var-node=storage-a"), wait_until="networkidle")
                await expect(page.get_by_text("Thin pools on storage-a", exact=True)).to_be_visible()
                await view.capture("single-node-narrow", animations="disabled")
                await asyncio.gather(*checks)
                assert not query_errors, query_errors


if __name__ == "__main__":
    pytest_bazel.main()
