"""Pinned Codex exec-server in the production Runner's private companion container."""

import http.client
import os
import shlex
import socket
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import grpc
import pytest
from python.runfiles import Runfiles
from tenacity import Retrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.env.e2e.testing import launches
from agentplane.env.runtime.execution.companion.control import control_pb2, control_pb2_grpc
from devinfra.oci.oci import load_oci_image
from third_party.containers import debian_slim
from util.testing.container_logs import LoggedContainer

# The generated gRPC stubs resolve protobuf's own stubs through direct deps.
# gazelle:include_dep @pypi//protobuf


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
    with grpc.insecure_channel(
        f"unix://{path}", options=(("grpc.enable_retries", 0), ("grpc.use_local_subchannel_pool", 1))
    ) as channel:
        status = control_pb2_grpc.CompanionControlStub(channel).Inspect(control_pb2.InspectRequest(), timeout=2)
        return CodexExecServerContainerStatus(generation=status.generation, owned=status.owned)


@pytest.fixture
def codex_exec_server_container(tmp_path: Path) -> Iterator[CodexExecServerContainer]:
    debian_image = load_oci_image(debian_slim.LAYOUT_RLOCATION)
    runfiles_root = Runfiles.CreateOrRaise().root()
    execution_binary = (runfiles_root / launches.EXECUTION_BINARY).resolve()
    codex_binary = Path(str(runfiles_root / launches.CODEX_BINARY)).resolve()
    socket_path = tmp_path / "executor.sock"
    with socket.socket() as executor_listener, socket.socket() as health_listener:
        executor_listener.bind(("127.0.0.1", 0))
        health_listener.bind(("127.0.0.1", 0))
        url = f"ws://127.0.0.1:{executor_listener.getsockname()[1]}"
        health_port = int(health_listener.getsockname()[1])
    container = (
        LoggedContainer(debian_image, test_name="runner-executor-companion")
        .with_kwargs(network_mode="host", mem_limit="512m", pids_limit=64, user=f"{os.getuid()}:{os.getgid()}")
        .with_volume_mapping(str(tmp_path), str(tmp_path), mode="rw")
        .with_volume_mapping(str(execution_binary), "/execution-service", mode="ro")
        .with_volume_mapping(str(codex_binary.parent.parent), "/opt/codex", mode="ro")
        .with_command(
            shlex.join(
                [
                    "/execution-service",
                    "companion",
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
        # Until the companion starts its ownership service, inspection is unavailable.
        Retrying(
            stop=stop_after_delay(10), wait=wait_fixed(0.05), retry=retry_if_exception_type(grpc.RpcError), reraise=True
        )(inspect_codex_exec_server_container, socket_path)
        companion = CodexExecServerContainer(url=url, socket=socket_path, container=container, health_port=health_port)
        companion.check_health()
        yield companion
