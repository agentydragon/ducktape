"""A runner process dies with its harnesses; the next one on the same state directory carries on."""

from __future__ import annotations

import asyncio
import ctypes
import errno
import os
import signal
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_bazel

from x.agentplane.harness_tests.scripted_upstream import ScriptedUpstream
from x.agentplane.runner import protocol_pb2 as pb
from x.agentplane.runner.client import RunnerClient
from x.agentplane.runner.testing import events, launches
from x.agentplane.runner.testing.scripted_model import ScriptedModel, Text

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf


@dataclass
class RunnerProcess:
    process: asyncio.subprocess.Process
    port: int

    @property
    def target(self) -> str:
        return f"127.0.0.1:{self.port}"

    async def crash(self, harness_pids: list[int]) -> None:
        """The runner dies without logging anything; its harnesses see stdin close and exit on their
        own, as they would when only the runner container is lost."""
        os.killpg(self.process.pid, signal.SIGKILL)
        await self.process.wait()
        for pid in harness_pids:
            await _exited(pid)

    async def stop(self) -> None:
        if self.process.returncode is None:
            self.process.send_signal(signal.SIGTERM)
            await self.process.wait()


# Bazel's Python is built without os.pidfd_open; the syscall itself is on every kernel this runs on.
_SYS_PIDFD_OPEN = 434


def _pidfd_open(pid: int) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    fd = int(libc.syscall(_SYS_PIDFD_OPEN, pid, 0))
    if fd < 0:
        code = ctypes.get_errno()
        raise (ProcessLookupError if code == errno.ESRCH else OSError)(code, os.strerror(code))
    return fd


async def _exited(pid: int) -> None:
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


@pytest.fixture
async def start_runner(
    provider: str, upstream: ScriptedUpstream, tmp_path: Path
) -> AsyncIterator[Callable[[], Awaitable[RunnerProcess]]]:
    started: list[RunnerProcess] = []

    async def start() -> RunnerProcess:
        environment = {
            **launches.environment(tmp_path / "home"),
            # The runner binary is a Bazel py_binary inside this test's runfiles tree and finds its
            # own runfiles through these.
            **{key: os.environ[key] for key in ("RUNFILES_DIR", "RUNFILES_MANIFEST_FILE") if key in os.environ},
            "ANTHROPIC_AUTH_TOKEN": launches.TOKEN,
            "OPENAI_API_KEY": launches.TOKEN,
        }
        process = await asyncio.create_subprocess_exec(
            *launches.runner_command(provider, upstream, state_dir=tmp_path / "state"),
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        assert process.stdout is not None
        line = (await process.stdout.readline()).decode()
        assert line.startswith("listening "), line
        runner = RunnerProcess(process, int(line.rsplit(":", 1)[1]))
        started.append(runner)
        return runner

    yield start
    for runner in started:
        await runner.stop()


async def test_a_restarted_runner_reports_the_loss_and_resumes_the_conversation(
    start_runner: Callable[[], Awaitable[RunnerProcess]], model: ScriptedModel, spec: pb.SessionSpec
) -> None:
    first_runner = await start_runner()
    client = RunnerClient(first_runner.target)
    first = await client.attach("restart-1", spec=spec)
    await first.send("input-1", "Reply with exactly: SEED_OK")
    request = await model.request()
    model.reply(request, Text("SEED_OK"))
    await first.until(events.turn_completed)
    await first.detach()
    await first.drain_until_end()
    await client.close()
    harness_pids = [event.harness_started.pid for event in events.of_kind(first.seen, "harness_started")]
    await first_runner.crash(harness_pids)

    second_runner = await start_runner()
    client = RunnerClient(second_runner.target)
    second = await client.attach("restart-1", after_sequence=first.cursor)
    lost = await second.until(events.is_kind("harness_lost"))
    started = await second.until(events.is_kind("harness_started"))
    assert lost.sequence < started.sequence
    assert started.harness_started.resumed
    assert not events.of_kind(second.seen, "turn_completed")
    await second.send("input-2", "Reply with exactly: RESUMED_OK")
    request = await model.request()
    assert request.user_texts == ["Reply with exactly: SEED_OK", "Reply with exactly: RESUMED_OK"]
    assert request.assistant_texts == ["SEED_OK"]
    model.reply(request, Text("RESUMED_OK"))
    done = await second.until(events.turn_completed)
    assert done.turn_completed.status == pb.TURN_STATUS_COMPLETED
    events.assert_contiguous([*first.seen, *second.seen])
    await second.shutdown()
    await second.drain_until_end()
    await client.close()
    model.assert_quiescent()


if __name__ == "__main__":
    pytest_bazel.main()
