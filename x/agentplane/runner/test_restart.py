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

from x.agentplane.harness_tests.claude.messages import AnthropicMessages
from x.agentplane.harness_tests.codex.responses import OpenAIResponses
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
            await asyncio.wait_for(_exited(pid), timeout=10)

    async def stop(self) -> None:
        """SIGTERM, as a Pod's termination sends it; the runner stops its harnesses before exiting."""
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
    harness: pb.Harness, endpoint: AnthropicMessages | OpenAIResponses, tmp_path: Path
) -> AsyncIterator[Callable[..., Awaitable[RunnerProcess]]]:
    started: list[RunnerProcess] = []

    async def start(*, test_debug_checkpoint: tuple[str, str] | None = None) -> RunnerProcess:
        environment = {
            **launches.environment(tmp_path / "home"),
            # The runner binary is a Bazel py_binary inside this test's runfiles tree and finds its
            # own runfiles through these.
            **{key: os.environ[key] for key in ("RUNFILES_DIR", "RUNFILES_MANIFEST_FILE") if key in os.environ},
            "ANTHROPIC_AUTH_TOKEN": launches.TOKEN,
            "OPENAI_API_KEY": launches.TOKEN,
        }
        process = await asyncio.create_subprocess_exec(
            *launches.runner_command(
                harness, endpoint.origin, state_dir=tmp_path / "state", test_debug_checkpoint=test_debug_checkpoint
            ),
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
    await model.reply(request, Text("SEED_OK"))
    await first.until(events.turn_completed)
    await first.detach()
    await first.drain_until_end()
    await client.close()
    harness_pids = [event.harness_started.pid for event in events.of_kind(first.seen, "harness_started")]
    await first_runner.crash(harness_pids)

    second_runner = await start_runner()
    client = RunnerClient(second_runner.target)
    second = await client.attach("restart-1", spec=spec, after_sequence=first.cursor)
    lost = await second.until(events.is_kind("harness_lost"))
    started = await second.until(events.is_kind("harness_started"))
    assert lost.sequence < started.sequence
    assert started.harness_started.resumed
    assert not events.of_kind(second.seen, "turn_completed")
    await second.send("input-2", "Reply with exactly: RESUMED_OK")
    request = await model.request()
    assert request.user_texts == ["Reply with exactly: SEED_OK", "Reply with exactly: RESUMED_OK"]
    assert request.assistant_texts == ["SEED_OK"]
    await model.reply(request, Text("RESUMED_OK"))
    done = await second.until(events.turn_completed)
    assert done.turn_completed.status == pb.TURN_STATUS_COMPLETED
    events.assert_contiguous([*first.seen, *second.seen])
    await second.stop_runner_session("stop-after-crash")
    await second.drain_until_end()
    await client.close()


async def test_crash_after_runner_receipt_before_native_dispatch_retries_once(
    start_runner: Callable[..., Awaitable[RunnerProcess]], model: ScriptedModel, spec: pb.SessionSpec
) -> None:
    """This is intentionally a real runner/harness process test, not a mocked Session method.

    The checkpoint is after the fsynced ``dispatch_planned`` journal record and before either native
    adapter receives the command. Killing the runner there proves a fresh runner delivers the same
    command once, with its original id, rather than dropping it or guessing a terminal result.
    """
    command_id = "retry-after-receipt"
    first_runner = await start_runner(test_debug_checkpoint=("after-dispatch-planned", command_id))
    client = RunnerClient(first_runner.target)
    first = await client.attach("restart-retry-1", spec=spec)
    # Both native harnesses make a durable resume point only after one completed turn. This is the
    # normal target for a later command and keeps the crash test about command recovery, not a
    # harness's first-thread bootstrap contract.
    await first.send("seed-before-retry", "Reply with exactly: RETRY_SEED_OK")
    seed_request = await model.request()
    await model.reply(seed_request, Text("RETRY_SEED_OK"))
    await first.until(events.turn_completed)
    await first.send(command_id, "Reply with exactly: RETRIED_ONCE_OK")
    await first.until(events.is_kind("command_received"))
    debug = await first.until(events.is_kind("debug_checkpoint"))
    assert (debug.debug_checkpoint.name, debug.debug_checkpoint.command_id) == ("after-dispatch-planned", command_id)
    harness_pids = [event.harness_started.pid for event in events.of_kind(first.seen, "harness_started")]
    await first_runner.crash(harness_pids)
    await client.close()

    # The first process is dead; the successor sees the persisted checkpoint and issues the one
    # native command. The upstream has seen no request at the first checkpoint.
    second_runner = await start_runner()
    client = RunnerClient(second_runner.target)
    second = await client.attach("restart-retry-1", spec=spec, after_sequence=first.cursor)
    request = await model.request()
    assert request.user_texts[-1] == "Reply with exactly: RETRIED_ONCE_OK"
    assert model.request_count == 2
    await model.reply(request, Text("RETRIED_ONCE_OK"))
    confirmed = await second.until(events.is_kind("harness_user_message_confirmed"))
    assert confirmed.harness_user_message_confirmed.origin_command_ids == [command_id]
    await second.until(events.turn_completed)
    await second.stop_runner_session("stop-after-retry")
    await second.drain_until_end()
    await client.close()


async def test_crash_after_terminal_effect_persists_replays_that_effect(
    start_runner: Callable[..., Awaitable[RunnerProcess]], model: ScriptedModel, spec: pb.SessionSpec
) -> None:
    """A journaled terminal outcome cannot disappear if the public Event append loses the process."""
    command_id = "replay-persisted-effect"
    # This case needs only the real harness/runner protocol path; let dispatch proceed to the
    # native confirmation gate, then crash before Session emits its public confirmation event.
    first_runner = await start_runner(test_debug_checkpoint=("after-terminal-outcome", command_id))
    client = RunnerClient(first_runner.target)
    first = await client.attach("restart-effect-1", spec=spec)
    await first.send("seed-before-effect", "Reply with exactly: EFFECT_SEED_OK")
    seed_request = await model.request()
    await model.reply(seed_request, Text("EFFECT_SEED_OK"))
    await first.until(events.turn_completed)
    await first.send(command_id, "Reply with exactly: EFFECT_REPLAY_OK")
    await first.until(events.is_kind("command_received"))
    # Claude's confirmation correlation is its first response-side `message_start`, unlike
    # Codex's turn-start acknowledgement. Feed the real upstream response before waiting for the
    # harness-neutral terminal-effect checkpoint: either harness may then reach the same durable
    # runner boundary without the test assuming a shared native ordering.
    target_request = await model.request()
    await model.reply(target_request, Text("EFFECT_REPLAY_OK"))
    debug = await first.until(events.is_kind("debug_checkpoint"))
    assert (debug.debug_checkpoint.name, debug.debug_checkpoint.command_id) == ("after-terminal-outcome", command_id)
    assert not [
        event
        for event in events.of_kind(first.seen, "harness_user_message_confirmed")
        if command_id in event.harness_user_message_confirmed.origin_command_ids
    ]
    # The runner has journaled the effect but is intentionally not allowed to append its public
    # Event. Killing it now isolates that journal/Event append window rather than a harness's
    # upstream-response boundary.
    harness_pids = [event.harness_started.pid for event in events.of_kind(first.seen, "harness_started")]
    await first_runner.crash(harness_pids)
    await client.close()

    second_runner = await start_runner()
    client = RunnerClient(second_runner.target)
    # Do not start a replacement harness here. This boundary proves the runner's own durable
    # event replay before a later continuation contract decides how to resume an interrupted native
    # turn; attaching without a spec is the protocol's diagnostic/replay path.
    second = await client.attach("restart-effect-1", after_sequence=first.cursor)
    confirmed = await second.until(events.is_kind("harness_user_message_confirmed"))
    assert confirmed.harness_user_message_confirmed.text == "Reply with exactly: EFFECT_REPLAY_OK"
    assert confirmed.harness_user_message_confirmed.origin_command_ids == [command_id]
    assert len(events.of_kind(second.seen, "harness_user_message_confirmed")) == 1
    await second.drain_until_end()
    await client.close()


async def test_sigterm_stops_the_harness_cleanly_and_the_next_runner_resumes(
    start_runner: Callable[[], Awaitable[RunnerProcess]], model: ScriptedModel, spec: pb.SessionSpec
) -> None:
    first_runner = await start_runner()
    client = RunnerClient(first_runner.target)
    first = await client.attach("sigterm-1", spec=spec)
    await first.send("input-1", "Reply with exactly: SEED_OK")
    await model.reply(await model.request(), Text("SEED_OK"))
    await first.until(events.turn_completed)
    (running,) = await client.list_sessions()
    assert running.session_id == "sigterm-1"
    assert running.harness_state == pb.HARNESS_STATE_RUNNING
    assert running.spec == spec
    # Native frames after the turn's result may still be arriving.
    assert running.last_sequence >= first.cursor
    await first.detach()
    await first.drain_until_end()
    await client.close()
    # The runner exits only after the stop ladder has reaped every harness.
    await first_runner.stop()

    second_runner = await start_runner()
    client = RunnerClient(second_runner.target)
    (stopped,) = await client.list_sessions()
    assert stopped.harness_state == pb.HARNESS_STATE_STOPPED
    assert stopped.last_sequence > first.cursor
    second = await client.attach("sigterm-1", spec=spec, after_sequence=first.cursor)
    exited = await second.until(events.is_kind("harness_exited"))
    assert exited.harness_exited.stopped_by_runner
    started = await second.until(events.is_kind("harness_started"))
    assert started.harness_started.resumed
    assert not events.of_kind(second.seen, "harness_lost")
    await second.send("input-2", "Reply with exactly: RESUMED_OK")
    request = await model.request()
    assert request.user_texts == ["Reply with exactly: SEED_OK", "Reply with exactly: RESUMED_OK"]
    await model.reply(request, Text("RESUMED_OK"))
    done = await second.until(events.turn_completed)
    assert done.turn_completed.status == pb.TURN_STATUS_COMPLETED
    events.assert_contiguous([*first.seen, *second.seen])
    await second.stop_runner_session("stop-after-sigterm")
    await second.drain_until_end()
    await client.close()


if __name__ == "__main__":
    pytest_bazel.main()
