"""Prove the Pod backend's outer containment boundary in a real PID namespace."""

import asyncio
import fcntl
import json
import os
import signal
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import override

import grpc
import pytest
import pytest_bazel
from python.runfiles import Runfiles
from tenacity import Retrying, retry_if_exception_type, stop_after_delay, wait_fixed
from testcontainers.core import testcontainers_config

from agentplane.env.config import Configuration, Endpoint, EnvironmentVariables, Harness, PodEnvironment
from agentplane.env.e2e.testing import launches
from agentplane.env.e2e.testing.gateway import Gateway
from agentplane.env.e2e.testing.process_fixtures import wait_for_exit
from agentplane.env.runtime.control import environment_runtime_pb2
from agentplane.environment_runtime_transport.config import Insecure
from agentplane.history_service import session_ingress_pb2, session_ingress_pb2_grpc
from agentplane.proto import session_pb2
from devinfra.oci.oci import load_oci_image
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir
from third_party.containers import debian_slim, ryuk
from util.testing.container_logs import LoggedContainer


def _load_images() -> str:
    load_oci_image(ryuk.LAYOUT_RLOCATION, image_ref=testcontainers_config.ryuk_image)
    return load_oci_image(debian_slim.LAYOUT_RLOCATION)


def _wait_for_markers(directory: Path, *names: str) -> None:
    """Wait for the fixture's marker files; the container's progress is in container.log."""
    for attempt in Retrying(
        stop=stop_after_delay(15), wait=wait_fixed(0.01), retry=retry_if_exception_type(AssertionError), reraise=True
    ):
        with attempt:
            missing = [name for name in names if not (directory / name).exists()]
            assert not missing, f"{missing=} in {directory}; see container.log"


def _descendant_pids(container: LoggedContainer) -> list[int]:
    table = container.get_wrapped_container().top()
    pid_index, command_index = table["Titles"].index("PID"), table["Titles"].index("CMD")
    rows = [row for row in table["Processes"] if "--pid1-proof-descendant" in row[command_index]]
    pids = [int(row[pid_index]) for row in rows]
    for pid in pids:
        # The Docker daemon and test share the outer PID namespace on RBE.
        assert b"--pid1-proof-descendant" in (Path("/proc") / str(pid) / "cmdline").read_bytes().split(b"\0")
    return pids


def _host_native_pid(container: LoggedContainer) -> int:
    table = container.get_wrapped_container().top()
    pid_index, command_index = table["Titles"].index("PID"), table["Titles"].index("CMD")
    rows = [row for row in table["Processes"] if "--pid1-proof-native host-setup" in row[command_index]]
    assert len(rows) == 1
    return int(rows[0][pid_index])


def _clear_generation(directory: Path) -> None:
    for path in directory.iterdir():
        path.unlink()


@pytest.fixture
async def acknowledging_history() -> AsyncIterator[str]:
    """Acknowledge the actual Host journal protocol without starting History's database."""
    acknowledgements: dict[tuple[str, str], session_ingress_pb2.Acknowledgement] = {}

    class Ingress(session_ingress_pb2_grpc.SessionJournalIngressServicer):
        @override
        async def Push(
            self, frames: AsyncIterator[session_ingress_pb2.PushFrame], context: grpc.aio.ServicerContext
        ) -> AsyncIterator[session_ingress_pb2.Acknowledgement]:
            first = await anext(frames)
            assert first.HasField("announce")
            assert first.announce.session_id
            key = (first.announce.session_id, first.announce.incarnation_uid)
            acknowledgement = acknowledgements.setdefault(key, session_ingress_pb2.Acknowledgement())
            yield session_ingress_pb2.Acknowledgement(cursor=acknowledgement.cursor, sealed=acknowledgement.sealed)
            async for frame in frames:
                if frame.HasField("entry"):
                    assert not acknowledgement.sealed
                    assert frame.entry.cursor == acknowledgement.cursor + 1
                    acknowledgement.cursor = frame.entry.cursor
                elif frame.HasField("sealed"):
                    assert frame.sealed.cursor == acknowledgement.cursor
                    assert frame.sealed.incarnation_uid
                    acknowledgement.sealed = True
                else:
                    assert frame.HasField("heartbeat")
                yield session_ingress_pb2.Acknowledgement(cursor=acknowledgement.cursor, sealed=acknowledgement.sealed)

    server = grpc.aio.server()
    session_ingress_pb2_grpc.add_SessionJournalIngressServicer_to_server(Ingress(), server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        yield f"127.0.0.1:{port}"
    finally:
        await server.stop(grace=None)


@pytest.mark.parametrize("mode", ["native-exit", "term", "escalate", "crash"])
async def test_pid1_container_fence_and_replacement(runfiles: Runfiles, tmp_path: Path, mode: str) -> None:
    debian_image = _load_images()
    binary = (
        runfiles.root() / "ducktape/agentplane/env/runtime/supervision/runner_pid1_fixture_/runner_pid1_fixture"
    ).resolve()
    container = (
        LoggedContainer(debian_image, test_name=f"runner-pid1-{mode}")
        .with_kwargs(user=f"{os.getuid()}:{os.getgid()}", pids_limit=64, mem_limit="256m")
        .with_volume_mapping(str(tmp_path), "/proof", mode="rw")
        .with_volume_mapping(str(binary), "/pid1-proof", mode="ro")
        .with_env("PID1_PROOF_MODE", mode)
        .with_command("/pid1-proof -test.run=^TestPID1ContainerContract$ -test.v")
    )
    with container:
        wrapped = container.get_wrapped_container()
        generations = []
        for generation in range(2):
            await asyncio.to_thread(_wait_for_markers, tmp_path, "ready")
            descendants = await asyncio.to_thread(_descendant_pids, container)
            assert len(descendants) == 2
            if mode == "native-exit":
                (tmp_path / "exit-native").write_text("exit\n")
            else:
                await asyncio.to_thread(wrapped.kill, signal="SIGKILL" if mode == "crash" else "SIGTERM")
            result = await asyncio.to_thread(wrapped.wait, timeout=15)
            assert result["StatusCode"] == (137 if mode == "crash" else 0)
            for pid in descendants:
                await asyncio.wait_for(wait_for_exit(pid), timeout=5)
            # Replacement requires observed container exit and descendant death.
            generations.append(
                {"generation": generation, "exit_code": result["StatusCode"], "descendants": descendants}
            )
            if generation == 0:
                await asyncio.to_thread(_clear_generation, tmp_path)
                await asyncio.to_thread(wrapped.start)
        (undeclared_outputs_dir() / f"runner-pid1-{mode}.json").write_text(json.dumps(generations, indent=2))


@pytest.mark.parametrize("abrupt", [False, True], ids=["term", "crash"])
async def test_production_runner_selects_pid1(
    runfiles: Runfiles, tmp_path: Path, gateway: Gateway, abrupt: bool
) -> None:
    debian_image = _load_images()
    runfiles_root = runfiles.root()
    runner = (runfiles_root / launches.RUNTIME_BINARY).resolve()
    fixture = (
        runfiles_root / "ducktape/agentplane/env/runtime/supervision/runner_pid1_fixture_/runner_pid1_fixture"
    ).resolve()
    host_control = gateway.endpoint
    assert isinstance(host_control.security, Insecure)
    config = Configuration(
        version=2,
        state_dir="/proof/state",
        initialization_cwd="/proof/state",
        control=host_control,
        ingress=Endpoint(target="127.0.0.1:8081", bearer="unused-ingress-token"),
        platform_instructions="PID1 proof",
        harness_environment=EnvironmentVariables(values={"PATH": "/bin:/usr/bin"}),
        environment=PodEnvironment(supervision="pid1"),
    )
    (tmp_path / "environment-runtime.json").write_text(config.model_dump_json(exclude_none=True))
    (tmp_path / "launch.json").write_text(
        json.dumps(
            {
                "sandbox": {"namespace": "proof", "name": "pid1-proof", "uid": "pid1-proof-uid"},
                "bootstrap": "exec /pid1-proof --pid1-proof-native crash" if abrupt else "printf ready > /proof/ready",
            }
        )
    )
    container = (
        LoggedContainer(debian_image, test_name=f"production-runner-pid1-{abrupt}")
        .with_kwargs(network_mode="host", user=f"{os.getuid()}:{os.getgid()}", pids_limit=128, mem_limit="256m")
        .with_volume_mapping(str(tmp_path), "/proof", mode="rw")
        .with_volume_mapping(str(runner), "/environment-runtime", mode="ro")
        .with_volume_mapping(str(fixture), "/pid1-proof", mode="ro")
        .with_env("OTEL_SDK_DISABLED", "true")
        .with_command("/environment-runtime --config /proof/environment-runtime.json --launch-file /proof/launch.json")
    )
    state = tmp_path / "state"
    state.mkdir()
    # The COSI database excludes a competing store while Runtime is alive.
    with container:
        wrapped = container.get_wrapped_container()
        await gateway.control.connect(host_control, "pid1-proof-uid")
        if abrupt:
            await asyncio.to_thread(_wait_for_markers, tmp_path, "native.pid", "group.pid", "escaped.pid")
            descendants = await asyncio.to_thread(_descendant_pids, container)
            assert len(descendants) == 2
        else:
            descendants = []
        with (state / "runtime-resources.db").open("rb") as probe:
            with pytest.raises(BlockingIOError):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            await asyncio.to_thread(wrapped.kill, signal="SIGKILL" if abrupt else "SIGTERM")
            result = await asyncio.to_thread(wrapped.wait, timeout=15)
            assert result["StatusCode"] == (137 if abrupt else 0)
            for pid in descendants:
                await asyncio.wait_for(wait_for_exit(pid), timeout=5)
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(probe, fcntl.LOCK_UN)
        (undeclared_outputs_dir() / f"production-runner-pid1-{abrupt}.json").write_text(json.dumps(result))


async def test_production_pid1_embedded_host_reaps_setup(
    runfiles: Runfiles, tmp_path: Path, gateway: Gateway, acknowledging_history: str
) -> None:
    """A Host-owned setup child must use Runtime's PID1 reaper and join its process group."""
    debian_image = _load_images()
    runfiles_root = runfiles.root()
    runner = (runfiles_root / launches.RUNTIME_BINARY).resolve()
    fixture = (
        runfiles_root / "ducktape/agentplane/env/runtime/supervision/runner_pid1_fixture_/runner_pid1_fixture"
    ).resolve()
    control = gateway.endpoint
    assert isinstance(control.security, Insecure)
    state = tmp_path / "state"
    state.mkdir()
    (state / "workspaces").mkdir()
    config = Configuration(
        version=2,
        state_dir="/proof/state",
        initialization_cwd="/proof/state",
        workspace_root="/proof/state/workspaces",
        control=control,
        ingress=Endpoint(target=acknowledging_history, bearer="unused-ingress-token"),
        platform_instructions="PID1 Host proof",
        harness_environment=EnvironmentVariables(
            values={"PATH": "/bin:/usr/bin", "HOME": "/proof/state", "OPENAI_API_KEY": "test-key"}
        ),
        codex=Harness(binary="/bin/false", base_url="http://127.0.0.1:8082/v1", token_env="OPENAI_API_KEY"),
        environment=PodEnvironment(supervision="pid1"),
    )
    (tmp_path / "environment-runtime.json").write_text(config.model_dump_json(exclude_none=True))
    (tmp_path / "launch.json").write_text(
        json.dumps({"sandbox": {"namespace": "proof", "name": "pid1-host", "uid": "pid1-host-uid"}})
    )
    container = (
        LoggedContainer(debian_image, test_name="production-pid1-embedded-host")
        .with_kwargs(network_mode="host", user=f"{os.getuid()}:{os.getgid()}", pids_limit=128, mem_limit="256m")
        .with_volume_mapping(str(tmp_path), "/proof", mode="rw")
        .with_volume_mapping(str(runner), "/environment-runtime", mode="ro")
        .with_volume_mapping(str(fixture), "/pid1-proof", mode="ro")
        .with_env("OTEL_SDK_DISABLED", "true")
        .with_env("OPENAI_API_KEY", "test-key")
        .with_command("/environment-runtime --config /proof/environment-runtime.json --launch-file /proof/launch.json")
    )
    with container:
        wrapped = container.get_wrapped_container()
        await gateway.control.connect(control, "pid1-host-uid")
        stub = await gateway.control.runner()
        await stub.CreateSession(
            environment_runtime_pb2.CreateSessionRequest(
                session_id=str(uuid.uuid4()),
                spec=session_pb2.SessionSpec(
                    placement=session_pb2.SessionPlacement(
                        environment_hosted=session_pb2.EnvironmentHostedPlacement(
                            environment=session_pb2.ExactEnvironment(name="pid1-host", uid="pid1-host-uid"),
                            harness=session_pb2.HARNESS_CODEX,
                        )
                    ),
                    cwd="/proof/state/workspaces/project",
                    model="pid1-proof-model",
                    reasoning_effort="low",
                ),
                setup_script="exec /pid1-proof --pid1-proof-native host-setup",
            )
        )
        await asyncio.to_thread(_wait_for_markers, tmp_path, "native.pid", "group.pid")
        descendants = await asyncio.to_thread(_descendant_pids, container)
        assert len(descendants) == 1
        os.kill(await asyncio.to_thread(_host_native_pid, container), signal.SIGUSR1)
        for pid in descendants:
            await asyncio.wait_for(wait_for_exit(pid), timeout=5)
        await asyncio.to_thread(wrapped.kill, signal="SIGTERM")
        try:
            result = await asyncio.to_thread(wrapped.wait, timeout=15)
        except Exception:
            # Preserve the management-process wait site in container.log.
            await asyncio.to_thread(wrapped.kill, signal="SIGQUIT")
            raise
        assert result["StatusCode"] == 0


if __name__ == "__main__":
    pytest_bazel.main()
