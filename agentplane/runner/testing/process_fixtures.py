"""Runner subprocess fixtures shared by lifecycle tests and experimental probes."""

from __future__ import annotations

import asyncio
import ctypes
import errno
import os
import signal
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from agentplane.history_service.testing.go_service import WRITER, GoHistoryService
from agentplane.native.claude.testing.messages import AnthropicMessages
from agentplane.native.codex.testing.responses import OpenAIResponses
from agentplane.proto import runner_pb2
from agentplane.runner.configuration import Endpoint
from agentplane.runner.testing import launches
from agentplane.runner.testing.gateway import Gateway

# gazelle:include_dep @pypi//protobuf


@dataclass
class RunnerProcess:
    process: asyncio.subprocess.Process

    async def crash(self, harness_pids: list[int]) -> None:
        """The runner dies without logging anything; its harnesses see stdin close and exit on their
        own, as they would when only the runner container is lost."""
        os.killpg(self.process.pid, signal.SIGKILL)
        await self.process.wait()
        for pid in harness_pids:
            await asyncio.wait_for(wait_for_exit(pid), timeout=10)

    async def stop(self) -> None:
        """SIGTERM, as a Pod's termination sends it; the runner stops its harnesses before exiting."""
        if self.process.returncode is None:
            self.process.send_signal(signal.SIGTERM)
            try:
                async with asyncio.timeout(20):
                    await self.process.wait()
            except TimeoutError:
                os.killpg(self.process.pid, signal.SIGKILL)
                await self.process.wait()
                raise TimeoutError("Go runner did not stop; a test checkpoint or script may still be held") from None


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


def runner_environment(home: Path) -> dict[str, str]:
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
    harness: runner_pb2.Harness,
    endpoint: AnthropicMessages | OpenAIResponses,
    tmp_path: Path,
    gateway: Gateway,
    history_service: GoHistoryService,
) -> AsyncIterator[Callable[..., Awaitable[RunnerProcess]]]:
    """Start a runner process on the test's state directory; it returns once the runner is connected."""
    started: list[RunnerProcess] = []
    ingress = Endpoint(target=history_service.ingress_target, bearer=await history_service.writer_token(WRITER))

    async def start(
        *,
        test_debug_checkpoint: tuple[str, str] | None = None,
        bootstrap: str | None = None,
        home: Path = tmp_path / "home",
        codex_binary: Path | None = None,
        harness_env: Sequence[str] = (),
        runner_binary: Path | None = None,
    ) -> RunnerProcess:
        connections = gateway.control.connections
        process = await asyncio.create_subprocess_exec(
            *launches.runner_command(
                harness,
                endpoint.origin,
                state_dir=tmp_path / "state",
                gateway=gateway.endpoint,
                ingress=ingress,
                test_debug_checkpoint=test_debug_checkpoint,
                bootstrap=bootstrap,
                codex_binary=codex_binary,
                harness_env=harness_env,
                runner_binary=runner_binary,
            ),
            env=runner_environment(home),
            start_new_session=True,
        )
        runner = RunnerProcess(process)
        started.append(runner)
        await gateway.control.wait_ready(connections + 1)
        return runner

    yield start
    for runner in started:
        await runner.stop()
