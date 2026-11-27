"""Exercise the production Blackbox modules against a local exporter socket."""

from __future__ import annotations

from pathlib import Path
from typing import cast
from urllib.parse import urlencode
from urllib.request import urlopen

import pytest_bazel
import yaml
from testcontainers.core.wait_strategies import HttpWaitStrategy

from cluster.cdk8s.monitoring import home_application_probes
from devinfra.oci.oci import load_oci_image
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from testing.container_logs import LoggedContainer
from third_party.containers import blackbox_exporter as blackbox_exporter_image

_PORT = 9115


def _probe(base_url: str, module: str, target: str) -> str:
    query = urlencode({"module": module, "target": target})
    with urlopen(f"{base_url}/probe?{query}", timeout=2) as response:
        return cast(str, response.read().decode())


def _probe_success(metrics: str) -> int:
    return int(next(line.split()[1] for line in metrics.splitlines() if line.startswith("probe_success ")))


def test_blackbox_modules_use_production_config(tmp_path: Path) -> None:
    image_ref = load_oci_image(blackbox_exporter_image.LAYOUT_RLOCATION)
    config_path = tmp_path / "blackbox.yml"
    config_path.write_text(yaml.safe_dump(home_application_probes.CONFIG))
    out_dir = undeclared_outputs_dir() / "home-application-probes"
    out_dir.mkdir(parents=True, exist_ok=True)

    with (
        LoggedContainer(
            image_ref,
            test_name="home-application-probes",
            command=[f"--config.file=/etc/blackbox_exporter/{config_path.name}"],
        )
        .with_volume_mapping(str(config_path), f"/etc/blackbox_exporter/{config_path.name}", "ro")
        .with_exposed_ports(_PORT) as container
    ):
        HttpWaitStrategy(_PORT, "/-/healthy").with_startup_timeout(20).wait_until_ready(container)
        base_url = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(_PORT)}"
        tcp_metrics = _probe(base_url, "tcp_ipv4", f"127.0.0.1:{_PORT}")
        http_on_https_metrics = _probe(base_url, "https_ipv4", f"http://127.0.0.1:{_PORT}/-/healthy")
        (out_dir / "probe-results.txt").write_text(
            "TCP exporter socket probe:\n"
            f"{tcp_metrics}\n"
            "Plain HTTP rejected by HTTPS module:\n"
            f"{http_on_https_metrics}\n"
        )

    assert _probe_success(tcp_metrics) == 1
    assert _probe_success(http_on_https_metrics) == 0


if __name__ == "__main__":
    pytest_bazel.main()
