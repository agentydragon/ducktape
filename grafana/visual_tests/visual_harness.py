"""Isolated Grafana and Prometheus fixture for dashboard visual tests."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from pathlib import Path

import docker
import httpx
from testcontainers.core import testcontainers_config
from testcontainers.core.network import Network

from devinfra.oci.oci import load_oci_image
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from grafana.visual_tests.recorded import prepare_recorded_dashboard_fixture
from third_party.containers import grafana_visual, prometheus_visual, ryuk
from util.testing.container_logs import LoggedContainer

TEST_PASSWORD = "isolated-synthetic-grafana"


def record_stage(stage: str) -> None:
    with (undeclared_outputs_dir() / "grafana-stages.log").open("a") as log:
        log.write(stage + "\n")


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
def _grafana_server(network: Network, grafana_image: str, datasources: Sequence[dict]) -> Iterator[str]:
    """Run isolated Grafana on a caller-owned network and provision its datasources."""
    grafana = (
        LoggedContainer(grafana_image, test_name="synthetic-grafana")
        .with_network(network)
        .with_envs(
            GF_AUTH_ANONYMOUS_ENABLED="true",
            GF_AUTH_ANONYMOUS_ORG_ROLE="Viewer",
            GF_ANALYTICS_REPORTING_ENABLED="false",
            GF_ANALYTICS_CHECK_FOR_UPDATES="false",
            # An empty preinstall list still installs Grafana's default plugins.
            GF_PLUGINS_PREINSTALL_DISABLED="true",
            GF_SECURITY_ADMIN_PASSWORD=TEST_PASSWORD,
            GF_USERS_DEFAULT_THEME="dark",
        )
    )
    with grafana:
        wrapped = grafana.get_wrapped_container()
        deadline = time.monotonic() + 20
        address = None
        while time.monotonic() < deadline:
            wrapped.reload()
            if wrapped.status in ("exited", "dead"):
                raise AssertionError(f"Grafana exited before joining the network: {wrapped.attrs['State']}")
            address = wrapped.attrs["NetworkSettings"].get("Networks", {}).get(network.name, {}).get("IPAddress")
            if address:
                break
            time.sleep(0.1)
        if not address:
            raise AssertionError(f"Grafana did not join its network: {wrapped.attrs['State']}")
        url = f"http://{address}:3000"
        record_stage("waiting for Grafana health")
        _wait_ready(f"{url}/api/health")
        record_stage("provisioning isolated Grafana datasources")
        with httpx.Client(base_url=url, timeout=15, auth=("admin", TEST_PASSWORD), trust_env=False) as http:
            for datasource in datasources:
                response = http.post("/api/datasources", json=datasource)
                response.raise_for_status()
        yield url


@contextmanager
def grafana_server(network: Network, datasources: Sequence[dict]) -> Iterator[str]:
    """Start isolated Grafana on an existing network with caller-provided datasources."""
    record_stage("loading isolated Grafana image")
    grafana_image = load_oci_image(grafana_visual.LAYOUT_RLOCATION)
    with _grafana_server(network, grafana_image, datasources) as url:
        yield url


@contextmanager
def grafana_dashboard(
    tmp_path: Path,
    dashboard_path: Path,
    samples: str,
    *,
    render_fixture: bool = False,
    variables: dict[str, list[str]] | None = None,
) -> Iterator[str]:
    record_stage("loading isolated service images")
    with ThreadPoolExecutor(max_workers=3) as executor:
        grafana_image = executor.submit(load_oci_image, grafana_visual.LAYOUT_RLOCATION)
        ryuk_image = executor.submit(load_oci_image, ryuk.LAYOUT_RLOCATION, image_ref=testcontainers_config.ryuk_image)
        prometheus_image = (
            None if render_fixture else executor.submit(load_oci_image, prometheus_visual.LAYOUT_RLOCATION)
        )
        grafana_ref = grafana_image.result()
        ryuk_image.result()
        prometheus_ref = prometheus_image.result() if prometheus_image is not None else None
    client = docker.from_env()
    data = tmp_path / "data"
    if prometheus_ref is not None:
        record_stage("creating synthetic TSDB")
        data.mkdir()
        (data / "fixture.om").write_text(samples)
        mount = {str(data): {"bind": "/fixture", "mode": "rw"}}
        client.containers.run(
            prometheus_ref,
            ["tsdb", "create-blocks-from", "openmetrics", "/fixture/fixture.om", "/fixture/tsdb"],
            entrypoint="/bin/promtool",
            volumes=mount,
            user="0",
            remove=True,
        )
    record_stage("starting Grafana" if render_fixture else "starting Prometheus and Grafana")
    try:
        with Network(docker_network_kw={"internal": True}) as network, ExitStack() as services:
            if prometheus_ref is not None:
                prometheus = (
                    LoggedContainer(prometheus_ref, test_name="synthetic-prometheus", user="0")
                    .with_network(network)
                    .with_network_aliases("prometheus")
                    .with_volume_mapping(data, "/fixture", "rw")
                    .with_command(
                        [
                            "--config.file=/etc/prometheus/prometheus.yml",
                            "--storage.tsdb.path=/fixture/tsdb",
                            "--storage.tsdb.retention.time=87600h",
                        ]
                    )
                )
                services.enter_context(prometheus)
            with _grafana_server(
                network,
                grafana_ref,
                [
                    {
                        "name": "Mimir",
                        "uid": "mimir",
                        "type": "prometheus",
                        "access": "proxy",
                        "url": "http://prometheus:9090",
                    }
                ],
            ) as url:
                with httpx.Client(base_url=url, timeout=15, auth=("admin", TEST_PASSWORD), trust_env=False) as http:
                    record_stage("provisioning dashboard")
                    dashboard = json.loads(dashboard_path.read_text())
                    dashboard["id"] = None
                    dashboard["version"] = 0
                    dashboard = json.loads(
                        json.dumps(dashboard)
                        .replace("${DS_MIMIR}", "Mimir")
                        .replace("${DS_PROMETHEUS}", "Mimir")
                        .replace("${DS_LOKI}", "Mimir" if render_fixture else "Loki")
                    )
                    if render_fixture:
                        prepare_recorded_dashboard_fixture(dashboard, variables or {})
                    response = http.post("/api/dashboards/db", json={"dashboard": dashboard, "overwrite": False})
                    response.raise_for_status()
                record_stage("dashboard ready")
                yield url
    finally:
        record_stage("stopping Grafana and Prometheus")
        client.close()
