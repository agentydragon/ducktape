"""Prove the Pod backend's outer containment boundary in a real PID namespace."""

import asyncio
import fcntl
import json
import os
import time
from pathlib import Path

import pytest
import pytest_bazel

from agentplane.runner.configuration import Configuration, Endpoint, EnvironmentVariables, PodEnvironment
from agentplane.runner.testing import launches
from agentplane.runner.testing.gateway import Gateway
from agentplane.runner.testing.process_fixtures import wait_for_exit
from third_party.containers import debian_slim
from util.bazel.runfiles import get_required_path
from util.oci import load_oci_image
from util.testing.container_logs import LoggedContainer
from util.testing.undeclared_outputs import undeclared_outputs_dir


def _wait_ready(directory: Path) -> None:
    deadline = time.monotonic() + 15
    while not (directory / "ready").exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("PID1 fixture did not become ready; see container.log")
        time.sleep(0.01)


def _descendant_pids(container: LoggedContainer, backend: str) -> list[int]:
    table = container.get_wrapped_container().top()
    pid_index, command_index = table["Titles"].index("PID"), table["Titles"].index("CMD")
    supervisors = [row for row in table["Processes"] if "--agentplane-runner-supervisor" in row[command_index]]
    assert len(supervisors) == (0 if backend == "pid1" else 1)
    rows = [row for row in table["Processes"] if "--pid1-proof-descendant" in row[command_index]]
    pids = [int(row[pid_index]) for row in rows]
    for pid in pids:
        # The Docker daemon and test share the outer PID namespace on RBE.
        assert b"--pid1-proof-descendant" in (Path("/proc") / str(pid) / "cmdline").read_bytes().split(b"\0")
    return pids


def _clear_generation(directory: Path) -> None:
    for path in directory.iterdir():
        if path.name != "owner":
            path.unlink()


@pytest.mark.parametrize("mode", ["native-exit", "term", "escalate", "crash"])
@pytest.mark.parametrize("backend", ["pid1", "subprocess"])
async def test_pid1_container_fence_and_replacement(tmp_path: Path, mode: str, backend: str) -> None:
    load_oci_image(debian_slim.IMAGE)
    binary = get_required_path("_main/agentplane/runner/supervision/runner_pid1_fixture_/runner_pid1_fixture").resolve()
    container = (
        LoggedContainer(debian_slim.IMAGE.tag, test_name=f"runner-pid1-{backend}-{mode}")
        .with_kwargs(user=f"{os.getuid()}:{os.getgid()}", pids_limit=64, mem_limit="256m")
        .with_volume_mapping(str(tmp_path), "/proof", mode="rw")
        .with_volume_mapping(str(binary), "/pid1-proof", mode="ro")
        .with_env("PID1_PROOF_MODE", mode)
        .with_env("PID1_PROOF_BACKEND", backend)
        .with_command("/pid1-proof -test.run=^TestPID1ContainerContract$ -test.v")
    )
    with container:
        wrapped = container.get_wrapped_container()
        generations = []
        for generation in range(2):
            await asyncio.to_thread(_wait_ready, tmp_path)
            descendants = await asyncio.to_thread(_descendant_pids, container, backend)
            assert len(descendants) == (1 if mode == "native-exit" else 2)
            with (tmp_path / "owner").open("rb") as probe:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if mode == "native-exit":
                    (tmp_path / "exit-native").write_text("exit\n")
                else:
                    await asyncio.to_thread(wrapped.kill, signal="SIGKILL" if mode == "crash" else "SIGTERM")
                result = await asyncio.to_thread(wrapped.wait, timeout=15)
                assert result["StatusCode"] == (137 if mode == "crash" else 0)
                for pid in descendants:
                    await asyncio.wait_for(wait_for_exit(pid), timeout=5)
                # The observed runtime exit, not flock availability, authorizes
                # replacement. Escaped descendants are fenced at this boundary.
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(probe, fcntl.LOCK_UN)
            generations.append(
                {"generation": generation, "exit_code": result["StatusCode"], "descendants": descendants}
            )
            if generation == 0:
                await asyncio.to_thread(_clear_generation, tmp_path)
                await asyncio.to_thread(wrapped.start)
        (undeclared_outputs_dir() / f"runner-pid1-{backend}-{mode}.json").write_text(json.dumps(generations, indent=2))


@pytest.mark.parametrize("abrupt", [False, True], ids=["term", "crash"])
async def test_production_runner_selects_pid1(tmp_path: Path, gateway: Gateway, abrupt: bool) -> None:
    load_oci_image(debian_slim.IMAGE)
    runner = get_required_path(launches.RUNNER_BINARY).resolve()
    fixture = get_required_path(
        "_main/agentplane/runner/supervision/runner_pid1_fixture_/runner_pid1_fixture"
    ).resolve()
    control = gateway.endpoint_for_sandbox("proof", "pid1-proof-uid")
    config = Configuration(
        version=2,
        state_dir="/proof/state",
        initialization_cwd="/proof/state",
        control=control,
        ingress=Endpoint(target=control.target, bearer="unused-ingress-token"),
        platform_instructions="PID1 proof",
        harness_environment=EnvironmentVariables(values={"PATH": "/bin:/usr/bin"}),
        environment=PodEnvironment(supervision="pid1"),
    )
    (tmp_path / "runner.json").write_text(config.model_dump_json(exclude_none=True))
    (tmp_path / "launch.json").write_text(
        json.dumps(
            {
                "sandbox": {"namespace": "proof", "name": "pid1-proof", "uid": "pid1-proof-uid"},
                "bootstrap": "exec /pid1-proof --pid1-proof-native crash" if abrupt else "printf ready > /proof/ready",
            }
        )
    )
    container = (
        LoggedContainer(debian_slim.IMAGE.tag, test_name=f"production-runner-pid1-{abrupt}")
        .with_kwargs(network_mode="host", user=f"{os.getuid()}:{os.getgid()}", pids_limit=128, mem_limit="256m")
        .with_volume_mapping(str(tmp_path), "/proof", mode="rw")
        .with_volume_mapping(str(runner), "/runner", mode="ro")
        .with_volume_mapping(str(fixture), "/pid1-proof", mode="ro")
        .with_env("OTEL_SDK_DISABLED", "true")
        .with_command("/runner --config /proof/runner.json --launch-file /proof/launch.json")
    )
    connections = gateway.control.connections
    with container:
        wrapped = container.get_wrapped_container()
        if abrupt:
            await asyncio.to_thread(_wait_native_tree, tmp_path)
            descendants = await asyncio.to_thread(_descendant_pids, container, "pid1")
            assert len(descendants) == 2
        else:
            await asyncio.wait_for(gateway.control.wait_connected(connections + 1), timeout=20)
            descendants = []
        with (tmp_path / "state" / ".agentplane-runner-owner").open("rb") as probe:
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


def _wait_native_tree(directory: Path) -> None:
    deadline = time.monotonic() + 15
    while not all((directory / f"{role}.pid").exists() for role in ("native", "group", "escaped")):
        if time.monotonic() >= deadline:
            raise TimeoutError("production Runner did not launch the native process tree; see container.log")
        time.sleep(0.01)


if __name__ == "__main__":
    pytest_bazel.main()
