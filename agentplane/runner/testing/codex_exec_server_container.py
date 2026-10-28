"""Pinned Codex exec-server in the production Runner's private companion container."""

import http.client
import json
import os
import shlex
import socket
import time
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import pytest

from agentplane.runner.testing import launches
from third_party.containers import debian_slim
from util.bazel.runfiles import get_required_path
from util.oci import load_oci_image
from util.testing.container_logs import LoggedContainer


@dataclass(frozen=True)
class CodexExecServerContainer:
    url: str
    socket: Path
    container: LoggedContainer
    health_port: int

    def check_health(self) -> None:
        with closing(http.client.HTTPConnection("127.0.0.1", self.health_port, timeout=2)) as connection:
            connection.request("GET", "/healthz")
            response = connection.getresponse()
            assert response.status == 200, f"companion health returned {response.status}: {response.read()!r}"


@dataclass(frozen=True)
class CodexExecServerContainerStatus:
    generation: str
    owned: bool


def inspect_codex_exec_server_container(path: Path) -> CodexExecServerContainerStatus:
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
    return CodexExecServerContainerStatus(generation=status["generation"], owned=status["owned"])


@pytest.fixture
def codex_exec_server_container(tmp_path: Path) -> Iterator[CodexExecServerContainer]:
    load_oci_image(debian_slim.IMAGE)
    companion_binary = get_required_path(launches.EXECUTOR_BINARY).resolve()
    codex_binary = get_required_path(launches.CODEX_BINARY).resolve()
    socket_path = tmp_path / "executor.sock"
    with socket.socket() as executor_listener, socket.socket() as health_listener:
        executor_listener.bind(("127.0.0.1", 0))
        health_listener.bind(("127.0.0.1", 0))
        url = f"ws://127.0.0.1:{executor_listener.getsockname()[1]}"
        health_port = int(health_listener.getsockname()[1])
    container = (
        LoggedContainer(debian_slim.IMAGE.tag, test_name="runner-executor-companion")
        .with_kwargs(network_mode="host", mem_limit="512m", pids_limit=64, user=f"{os.getuid()}:{os.getgid()}")
        .with_volume_mapping(str(tmp_path), str(tmp_path), mode="rw")
        .with_volume_mapping(str(companion_binary), "/runner", mode="ro")
        .with_volume_mapping(str(codex_binary.parent.parent), "/opt/codex", mode="ro")
        .with_command(
            shlex.join(
                [
                    "/runner",
                    "--socket",
                    str(socket_path),
                    "--listen",
                    url,
                    "--binary",
                    "/opt/codex/bin/codex",
                    "--health-listen",
                    f"127.0.0.1:{health_port}",
                ]
            )
        )
    )
    with container:
        deadline = time.monotonic() + 10
        while True:
            try:
                inspect_codex_exec_server_container(socket_path)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("executor companion did not create its ownership socket") from None
                time.sleep(0.05)
        companion = CodexExecServerContainer(url=url, socket=socket_path, container=container, health_port=health_port)
        companion.check_health()
        yield companion
