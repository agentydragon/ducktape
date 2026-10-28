"""Pinned Codex exec-server in the production Runner's private companion container."""

import json
import os
import shlex
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from agentplane.runner.testing import launches
from third_party.containers import debian_slim
from util.bazel.runfiles import get_required_path
from util.oci import load_oci_image
from util.testing.container_logs import LoggedContainer


@dataclass(frozen=True)
class ExecutorCompanion:
    url: str
    socket: Path
    health_url: str
    container: LoggedContainer


@dataclass(frozen=True)
class ExecutorStatus:
    generation: str
    owned: bool


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def inspect_executor(path: Path) -> ExecutorStatus:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(2)
        connection.connect(str(path))
        connection.sendall(b'{"operation":"inspect"}\n')
        response = bytearray()
        while not response.endswith(b"\n"):
            chunk = connection.recv(4096)
            if not chunk:
                raise ConnectionError("executor companion closed its inspect connection")
            response.extend(chunk)
    status = json.loads(response)
    return ExecutorStatus(generation=status["generation"], owned=status["owned"])


def executor_health_ok(health_url: str) -> bool:
    try:
        with urllib.request.urlopen(health_url, timeout=2) as response:
            return response.status == 200
    except OSError, urllib.error.URLError:
        return False


@pytest.fixture
def executor_companion(tmp_path: Path) -> Iterator[ExecutorCompanion]:
    load_oci_image(debian_slim.IMAGE)
    runner_binary = get_required_path(launches.RUNNER_BINARY).resolve()
    codex_binary = get_required_path(launches.CODEX_BINARY).resolve()
    socket_path = tmp_path / "executor.sock"
    url = f"ws://127.0.0.1:{_free_port()}"
    health_port = _free_port()
    health_url = f"http://127.0.0.1:{health_port}/healthz"
    container = (
        LoggedContainer(debian_slim.IMAGE.tag, test_name="runner-executor-companion")
        .with_kwargs(network_mode="host", mem_limit="512m", pids_limit=64, user=f"{os.getuid()}:{os.getgid()}")
        .with_volume_mapping(str(tmp_path), str(tmp_path), mode="rw")
        .with_volume_mapping(str(runner_binary), "/runner", mode="ro")
        .with_volume_mapping(str(codex_binary.parent.parent), "/opt/codex", mode="ro")
        .with_command(
            shlex.join(
                [
                    "/runner",
                    "--executor-container",
                    "--socket",
                    str(socket_path),
                    "--listen",
                    url,
                    "--health-listen",
                    f"0.0.0.0:{health_port}",
                    "--binary",
                    "/opt/codex/bin/codex",
                ]
            )
        )
    )
    with container:
        deadline = time.monotonic() + 10
        while True:
            try:
                inspect_executor(socket_path)
                if not executor_health_ok(health_url):
                    raise ConnectionError("executor health endpoint is not ready")
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("executor companion did not create its ownership socket") from None
                time.sleep(0.05)
        yield ExecutorCompanion(url=url, socket=socket_path, health_url=health_url, container=container)
