"""Prove the Pod backend's outer containment boundary in a real PID namespace."""

import asyncio
import fcntl
import json
import os
import time
from pathlib import Path

import pytest
import pytest_bazel

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


def _descendant_pids(container: LoggedContainer) -> list[int]:
    table = container.get_wrapped_container().top()
    pid_index, command_index = table["Titles"].index("PID"), table["Titles"].index("CMD")
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


@pytest.mark.parametrize("mode", ["native-exit", "term", "crash"])
async def test_pid1_container_fence_and_replacement(tmp_path: Path, mode: str) -> None:
    load_oci_image(debian_slim.IMAGE)
    binary = get_required_path("_main/agentplane/runner/supervision/runner_pid1_fixture_/runner_pid1_fixture").resolve()
    container = (
        LoggedContainer(debian_slim.IMAGE.tag, test_name=f"runner-pid1-{mode}")
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
            await asyncio.to_thread(_wait_ready, tmp_path)
            descendants = await asyncio.to_thread(_descendant_pids, container)
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
        (undeclared_outputs_dir() / f"runner-pid1-{mode}.json").write_text(json.dumps(generations, indent=2))


if __name__ == "__main__":
    pytest_bazel.main()
