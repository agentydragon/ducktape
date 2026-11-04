"""Runner subprocess fixtures shared by lifecycle tests and experimental probes."""

from __future__ import annotations

import asyncio
import ctypes
import errno
import fcntl
import json
import os
import shlex
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest

from agentplane.env.config import Endpoint
from agentplane.env.e2e.testing import launches
from agentplane.env.e2e.testing.gateway import Gateway
from agentplane.history_service.testing.go_service import WRITER, GoHistoryService
from agentplane.native.claude.testing.messages import AnthropicMessages
from agentplane.native.codex.testing.responses import OpenAIResponses
from agentplane.proto import session_pb2
from third_party.containers import debian_slim, ryuk
from util.oci import load_oci_image
from util.testing.container_logs import LoggedContainer

# gazelle:include_dep @pypi//protobuf


@dataclass
class RunnerProcess:
    container: LoggedContainer
    exit_code: int | None = None

    def _host_pids(self, namespace_pids: list[int]) -> list[int]:
        """Translate Runtime's PID-namespace IDs before its container exits."""
        table = self.container.get_wrapped_container().top()
        pid_index = table["Titles"].index("PID")
        host_pids = []
        for row in table["Processes"]:
            host_pid = int(row[pid_index])
            try:
                status = (Path("/proc") / str(host_pid) / "status").read_text()
            except FileNotFoundError:
                continue
            nspid = next(line for line in status.splitlines() if line.startswith("NSpid:"))
            if int(nspid.split()[-1]) in namespace_pids:
                host_pids.append(host_pid)
        return host_pids

    async def wait(self) -> int:
        if self.exit_code is None:
            result = await asyncio.to_thread(self.container.get_wrapped_container().wait, timeout=20)
            self.exit_code = result["StatusCode"]
        return self.exit_code

    async def crash(self, harness_pids: list[int]) -> None:
        """Kill Pod PID 1 and observe the container fence before starting a successor."""
        host_pids = await asyncio.to_thread(self._host_pids, harness_pids)
        if harness_pids:
            assert host_pids, f"none of the requested namespace PIDs were still in the container: {harness_pids}"
        await asyncio.to_thread(self.container.get_wrapped_container().kill, signal="SIGKILL")
        assert await self.wait() == 137
        for pid in host_pids:
            await asyncio.wait_for(wait_for_exit(pid), timeout=10)

    async def stop(self) -> None:
        """SIGTERM, as a Pod's termination sends it; the runner stops its harnesses before exiting."""
        if self.exit_code is None:
            await asyncio.to_thread(self.container.get_wrapped_container().kill, signal="SIGTERM")
            try:
                async with asyncio.timeout(20):
                    assert await self.wait() == 0
            except TimeoutError:
                await asyncio.to_thread(self.container.get_wrapped_container().kill, signal="SIGKILL")
                await self.wait()
                raise TimeoutError("Go runner did not stop; a test checkpoint or script may still be held") from None


async def wait_for_owner_release(state_dir: Path) -> None:
    # A native leader can exit before its supervisor finishes fencing descendants.
    # Wait for their inherited Runtime fence before a successor acquires ownership.
    try:
        async with asyncio.timeout(10):
            with (state_dir / ".agentplane-environment-runtime-owner").open("rb") as probe:
                while True:
                    try:
                        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        await asyncio.sleep(0.02)
                        continue
                    fcntl.flock(probe, fcntl.LOCK_UN)
                    break
    except TimeoutError:
        raise TimeoutError("supervised descendants retained the environment owner lock after crash") from None


# Bazel's Python is built without os.pidfd_open; the syscall itself is on every kernel this runs on.
_SYS_PIDFD_OPEN = 434


def _pidfd_open(pid: int) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    fd = int(libc.syscall(_SYS_PIDFD_OPEN, pid, 0))
    if fd < 0:
        code = ctypes.get_errno()
        # EINVAL: the pid survives only as a process group or session id, its leader already gone.
        raise (ProcessLookupError if code in {errno.ESRCH, errno.EINVAL} else OSError)(code, os.strerror(code))
    return fd


async def wait_for_exit(pid: int) -> None:
    """Wait for a process that is not our child: its pidfd becomes readable when it exits."""
    try:
        fd = _pidfd_open(pid)
    except ProcessLookupError:
        return
    loop = asyncio.get_running_loop()
    exited = loop.create_future()
    loop.add_reader(fd, exited.set_result, None)
    try:
        await exited
    finally:
        loop.remove_reader(fd)
        os.close(fd)


def environment_runtime_environment(home: Path) -> dict[str, str]:
    return {
        **launches.environment(home),
        # Keep native executable runfiles available to the test launcher.
        **{key: os.environ[key] for key in ("RUNFILES_DIR", "RUNFILES_MANIFEST_FILE") if key in os.environ},
        "ANTHROPIC_AUTH_TOKEN": launches.TOKEN,
        "OPENAI_API_KEY": launches.TOKEN,
        "OTEL_SDK_DISABLED": "true",
    }


@pytest.fixture
async def start_runner(
    harness: session_pb2.Harness,
    endpoint: AnthropicMessages | OpenAIResponses,
    tmp_path: Path,
    gateway: Gateway,
    history_service: GoHistoryService,
) -> AsyncIterator[Callable[..., Awaitable[RunnerProcess]]]:
    """Start a runner process on the test's state directory; it returns once the runner is connected."""
    started: list[RunnerProcess] = []
    ingress = Endpoint(target=history_service.ingress_target, bearer=await history_service.writer_token(WRITER))
    for image in (ryuk.IMAGE, debian_slim.IMAGE):
        load_oci_image(image)

    async def start(
        *,
        test_debug_checkpoint: tuple[str, str] | None = None,
        bootstrap: str | None = None,
        home: Path = tmp_path / "home",
        codex_binary: Path | None = None,
        harness_env: Sequence[str] = (),
        runner_binary: Path | None = None,
    ) -> RunnerProcess:
        command = launches.runner_command(
            harness,
            endpoint.origin,
            state_dir=tmp_path / "state",
            control=gateway.endpoint,
            ingress=ingress,
            test_debug_checkpoint=test_debug_checkpoint,
            bootstrap=bootstrap,
            codex_binary=codex_binary,
            harness_env=harness_env,
            runner_binary=runner_binary,
        )
        env = environment_runtime_environment(home)
        env["PATH"] = "/usr/bin:/bin"
        config = json.loads(await asyncio.to_thread(Path(command[2]).read_text))
        runtime = await asyncio.to_thread(Path(command[0]).absolute)
        native = await asyncio.to_thread(
            Path(config["claude" if harness == session_pb2.HARNESS_CLAUDE else "codex"]["binary"]).absolute
        )
        container = (
            LoggedContainer(debian_slim.IMAGE.tag, test_name=f"restart-runtime-{uuid4().hex}")
            .with_kwargs(network_mode="host", user=f"{os.getuid()}:{os.getgid()}", pids_limit=512, mem_limit="1g")
            .with_volume_mapping(str(tmp_path), str(tmp_path), mode="rw")
            .with_volume_mapping(str(runtime.resolve()), str(runtime), mode="ro")
            .with_command(shlex.join(command))
        )
        # Codex ships resources next to bin/codex; Claude is a single executable.
        native_mount = native.parent.parent if harness == session_pb2.HARNESS_CODEX else native
        container.with_volume_mapping(str(native_mount.resolve()), str(native_mount), mode="ro")
        for name, value in env.items():
            container.with_env(name, value)
        await asyncio.to_thread(container.__enter__)
        runner = RunnerProcess(container)
        started.append(runner)
        connected = asyncio.create_task(gateway.control.connect(gateway.endpoint, launches.SANDBOX.uid))
        exited = asyncio.create_task(runner.wait())
        try:
            done, _ = await asyncio.wait((connected, exited), return_when=asyncio.FIRST_COMPLETED)
            if exited in done:
                raise RuntimeError(f"Go runner exited with {exited.result()}")
            await connected
        except BaseException:
            await runner.stop()
            raise
        finally:
            connected.cancel()
            exited.cancel()
            await asyncio.gather(connected, exited, return_exceptions=True)
        return runner

    yield start
    for runner in started:
        try:
            await runner.stop()
        finally:
            await asyncio.to_thread(runner.container.__exit__, None, None, None)
    await gateway.control.disconnect()
