"""Send Talos JSON through the production Alloy pipeline into Loki's real API."""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

import pytest_bazel
from python.runfiles import Runfiles
from testcontainers.core.network import Network
from testcontainers.core.wait_strategies import HttpWaitStrategy

from devinfra.oci.oci import load_oci_image
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from testing.container_logs import LoggedContainer
from third_party.containers import alloy_config, loki_test

_LOKI_CONFIG = """\
auth_enabled: false
server:
  http_listen_port: 3100
common:
  instance_addr: 127.0.0.1
  path_prefix: /tmp/loki
  replication_factor: 1
  ring:
    kvstore:
      store: inmemory
  storage:
    filesystem:
      chunks_directory: /tmp/loki/chunks
      rules_directory: /tmp/loki/rules
schema_config:
  configs:
    - from: 2020-01-01
      store: tsdb
      object_store: filesystem
      schema: v13
      index:
        prefix: index_
        period: 24h
limits_config:
  discover_service_name: []
  discover_log_levels: false
analytics:
  reporting_enabled: false
"""


def _ready_loki_url(loki: LoggedContainer) -> str:
    HttpWaitStrategy(3100, "/ready").with_startup_timeout(30).wait_until_ready(loki)
    return f"http://{loki.get_container_host_ip()}:{loki.get_exposed_port(3100)}"


def _connect(host: str, port: int) -> socket.socket:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            return socket.create_connection((host, port), timeout=2)
        except OSError:
            time.sleep(0.2)
    raise AssertionError("Alloy did not listen; see talos-alloy/container.log")


def _entries(base_url: str, start_ns: int, count: int) -> list[tuple[int, str]]:
    query = urlencode(
        {"query": '{job="talos-node",node="test-talos"}', "start": start_ns, "direction": "forward", "limit": 100}
    )
    deadline = time.monotonic() + 20
    entries: list[tuple[int, str]] = []
    while time.monotonic() < deadline:
        with urlopen(f"{base_url}/loki/api/v1/query_range?{query}", timeout=3) as response:
            streams = json.load(response)["data"]["result"]
        entries = []
        for stream in streams:
            assert stream["stream"] == {"job": "talos-node", "node": "test-talos"}
            entries.extend((int(timestamp), line) for timestamp, line in stream["values"])
        if len(entries) >= count:
            return entries
        time.sleep(0.2)
    raise AssertionError(f"Expected {count} events, received {len(entries)}; see collector logs")


def test_talos_tcp_delivery(tmp_path: Path, runfiles: Runfiles) -> None:
    config = runfiles.root() / "ducktape/cluster/cdk8s/monitoring/talos.alloy"
    loki_config = tmp_path / "loki.yaml"
    loki_config.write_text(_LOKI_CONFIG)
    alloy_image = load_oci_image(alloy_config.LAYOUT_RLOCATION)
    loki_image = load_oci_image(loki_test.LAYOUT_RLOCATION)
    # The Talos sender includes its event time as a JSON field. Vector used receipt
    # time for Loki; keep that distinction, including for old/out-of-order events.
    lines = [
        json.dumps(
            {"talos-service": "machined", "talos-level": "info", "talos-time": "2020-01-01T00:00:00Z", "msg": msg},
            ensure_ascii=False,
        )
        for msg in ("split TCP writes: ☃\nescaped newline", "long line " + "x" * 70_000, "after Loki restart")
    ]
    with (
        Network() as network,
        LoggedContainer(loki_image, test_name="talos-loki")
        .with_network(network)
        .with_network_aliases("loki")
        .with_exposed_ports(3100)
        .with_volume_mapping(str(loki_config), "/etc/loki/config.yaml", "ro")
        .with_command("-config.file=/etc/loki/config.yaml") as loki,
        LoggedContainer(alloy_image, test_name="talos-alloy")
        .with_kwargs(user="473:473", cap_drop=["ALL"], security_opt=["no-new-privileges:true"], mem_limit="256m")
        .with_network(network)
        .with_exposed_ports(13333)
        .with_volume_mapping(str(config), "/etc/alloy/talos.alloy", "ro")
        .with_env("TALOS_LISTEN_ADDRESS", "0.0.0.0:13333")
        .with_env("NODE_NAME", "test-talos")
        .with_env("LOKI_PUSH_URL", "http://loki:3100/loki/api/v1/push")
        .with_command(
            "run /etc/alloy/talos.alloy --stability.level=experimental "
            "--storage.path=/tmp/alloy --server.http.listen-addr=127.0.0.1:12347 --disable-reporting"
        ) as alloy,
    ):
        base_url = _ready_loki_url(loki)
        host, port = alloy.get_container_host_ip(), int(alloy.get_exposed_port(13333))
        start_ns = time.time_ns()
        with _connect(host, port) as connection:
            payload = (lines[0] + "\n" + lines[1] + "\n").encode()
            # Split inside the JSON, then coalesce two messages in the next write.
            connection.sendall(payload[:17])
            connection.sendall(payload[17:])
        assert [line for _, line in _entries(base_url, start_ns, 2)] == lines[:2]

        loki.get_wrapped_container().stop(timeout=2)
        with _connect(host, port) as connection:
            # Reconnect and exercise the final unterminated line at EOF as well.
            connection.sendall(lines[2].encode())
        # Allow a batch attempt while Loki is down, then exercise the writer's retry.
        time.sleep(2)
        loki.get_wrapped_container().start()
        # Docker can allocate a new ephemeral host port when a container restarts.
        try:
            base_url = _ready_loki_url(loki)
        finally:
            (undeclared_outputs_dir() / "loki-restart.log").write_bytes(loki.get_wrapped_container().logs())
        entries = _entries(base_url, start_ns, 3)
        assert [line for _, line in entries] == lines
        assert all(start_ns <= timestamp <= time.time_ns() for timestamp, _ in entries)


if __name__ == "__main__":
    pytest_bazel.main()
