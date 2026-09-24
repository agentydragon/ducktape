"""A runner process dies with its harnesses; the next one on the same state directory carries on."""

from __future__ import annotations

import asyncio
import ctypes
import errno
import os
import signal
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_bazel

from agentplane.harness_tests.claude.messages import AnthropicMessages
from agentplane.harness_tests.codex.responses import OpenAIResponses
from agentplane.protocol import event_pb2
from agentplane.runner import protocol_pb2
from agentplane.runner.client import RunnerClient
from agentplane.runner.harness_process import HarnessProcess
from agentplane.runner.store import StateOwner
from agentplane.runner.testing import events, launches
from agentplane.runner.testing.scripted_model import ScriptedModel, Text
from util.bazel.runfiles import get_required_path, own_repo_rlocation

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


def _runner_environment(tmp_path: Path) -> dict[str, str]:
    return {
        **launches.environment(tmp_path / "home"),
        # The runner binary is a Bazel py_binary inside this test's runfiles tree and finds its
        # own runfiles through these.
        **{key: os.environ[key] for key in ("RUNFILES_DIR", "RUNFILES_MANIFEST_FILE") if key in os.environ},
        "ANTHROPIC_AUTH_TOKEN": launches.TOKEN,
        "OPENAI_API_KEY": launches.TOKEN,
    }


@pytest.fixture
async def start_runner(
    harness: protocol_pb2.Harness, endpoint: AnthropicMessages | OpenAIResponses, tmp_path: Path
) -> AsyncIterator[Callable[..., Awaitable[RunnerProcess]]]:
    started: list[RunnerProcess] = []

    async def start(*, test_debug_checkpoint: tuple[str, str] | None = None) -> RunnerProcess:
        process = await asyncio.create_subprocess_exec(
            *launches.runner_command(
                harness, endpoint.origin, state_dir=tmp_path / "state", test_debug_checkpoint=test_debug_checkpoint
            ),
            env=_runner_environment(tmp_path),
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


async def test_competing_runner_cannot_take_state_or_dispatch_before_replacement(
    harness: protocol_pb2.Harness,
    endpoint: AnthropicMessages | OpenAIResponses,
    model: ScriptedModel,
    spec: protocol_pb2.SessionSpec,
    start_runner: Callable[..., Awaitable[RunnerProcess]],
    tmp_path: Path,
) -> None:
    """A retained-state owner fences a contender until its native child is gone."""
    command_id = "fenced-replacement"
    first_runner = await start_runner(test_debug_checkpoint=("after-dispatch-planned", command_id))
    client = RunnerClient(first_runner.target, capture_history=True)
    first = await client.attach("writer-handoff-1", spec=spec)
    await first.send("handoff-seed", "Reply with exactly: HANDOFF_SEED_OK")
    await model.reply(await model.request(), Text("HANDOFF_SEED_OK"))
    await first.until(events.turn_completed)
    await first.send(command_id, "Reply with exactly: HANDOFF_REPLACEMENT_OK")
    await first.until(events.is_kind("command_admitted"))
    checkpoint = await first.until(events.is_kind("debug_checkpoint"))
    harness_pids = [entry.event.harness_started.pid for entry in events.of_kind(first.seen, "harness_started")]

    contender = await asyncio.create_subprocess_exec(
        *launches.runner_command(harness, endpoint.origin, state_dir=tmp_path / "state"),
        env=_runner_environment(tmp_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    stdout, stderr = await contender.communicate()
    assert contender.returncode != 0
    assert stdout == b""
    assert "runner state directory" in stderr.decode()
    assert "already owned" in stderr.decode()

    await first_runner.crash(harness_pids)
    await client.close()

    replacement_runner = await start_runner()
    client = RunnerClient(replacement_runner.target, capture_history=True)
    replacement = await client.attach("writer-handoff-1", spec=spec, after_cursor=checkpoint.cursor)
    lost = await replacement.until(events.is_kind("harness_lost"))
    # The checkpoint pauses command dispatch, not the independent native-output reader.
    assert lost.cursor > checkpoint.cursor
    assert lost.origin.source_id == checkpoint.origin.source_id
    request = await model.request()
    assert request.user_texts[-1] == "Reply with exactly: HANDOFF_REPLACEMENT_OK"
    assert model.request_count == 2
    await model.reply(request, Text("HANDOFF_REPLACEMENT_OK"))
    confirmed = await replacement.until(events.is_kind("harness_user_message_confirmed"))
    assert confirmed.event.harness_user_message_confirmed.origin_command_ids == [command_id]
    await replacement.until(events.turn_completed)
    await replacement.stop_runner_session("stop-after-writer-handoff")
    await replacement.drain_until_end()
    events.assert_contiguous([*first.seen, *replacement.seen])
    await client.close()


async def test_state_fence_survives_native_leader_exit_until_its_child_group_is_stopped(
    harness: protocol_pb2.Harness,
    endpoint: AnthropicMessages | OpenAIResponses,
    start_runner: Callable[..., Awaitable[RunnerProcess]],
    tmp_path: Path,
) -> None:
    """A background native tool retains the inherited fence after its harness leader exits."""
    state_dir = tmp_path / "state"
    owner = StateOwner(state_dir)
    process = HarnessProcess(
        [str(get_required_path(own_repo_rlocation("agentplane/runner/harness_background_child_testonly")))],
        cwd=tmp_path,
        environment={},
        state_owner_descriptor=owner.descriptor,
    )
    tool_pid: int | None = None
    owner_descriptor_closed = False
    try:
        await process.start()
        tool_pid = int((await anext(process.line_batches()))[0])
        # Closing a dead runner's descriptor does not unlock the shared open file description
        # inherited by the supervisor/native group. ``StateOwner.close`` is intentionally not
        # used: its orderly-shutdown unlock would release that shared lock for every descendant.
        os.close(owner.descriptor)
        owner_descriptor_closed = True

        contender = await asyncio.create_subprocess_exec(
            *launches.runner_command(harness, endpoint.origin, state_dir=state_dir),
            env=_runner_environment(tmp_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            # Either failure reaches EOF without ever listening, or an ownership regression
            # immediately exposes a listener. Neither case needs a timing-based probe.
            assert contender.stdout is not None
            assert contender.stderr is not None
            assert await contender.stdout.readline() == b""
            stderr = await contender.stderr.read()
            await contender.wait()
            assert contender.returncode != 0
            assert "runner state directory" in stderr.decode()
            assert "already owned" in stderr.decode()
        finally:
            if contender.returncode is None:
                os.killpg(contender.pid, signal.SIGKILL)
                await contender.wait()

        # SIGUSR1 is the supervisor's parent-death signal. The native leader exits on its
        # graceful group SIGTERM, while this tool ignores it; the supervisor must then force-stop
        # the remaining group before its inherited lock can be released.
        os.killpg(process.process.pid, signal.SIGUSR1)
        await asyncio.wait_for(_exited(tool_pid), timeout=10)
        # A successor can acquire the retained state only after the supervisor has dropped the
        # same inherited descriptor. This is stronger than waiting on its stdout-owning process.
        successor = await start_runner()
        await successor.stop()
    finally:
        if not owner_descriptor_closed:
            owner.close()
        if tool_pid is not None:
            with suppress(ProcessLookupError):
                os.killpg(process.native_pid, signal.SIGKILL)


async def test_native_leader_exit_stops_background_tool(tmp_path: Path) -> None:
    """Leader exit must fence tools even while the runner (this test process) stays alive."""
    owner = StateOwner(tmp_path / "state")
    process = HarnessProcess(
        [str(get_required_path(own_repo_rlocation("agentplane/runner/harness_background_child_testonly")))],
        cwd=tmp_path,
        environment={},
        state_owner_descriptor=owner.descriptor,
    )
    try:
        await process.start()
        try:
            tool_pid = int((await anext(process.line_batches()))[0])
            # Only the leader dies: no stop/parent-death signal reaches its supervisor.
            os.kill(process.native_pid, signal.SIGKILL)
            async with asyncio.timeout(10):
                await _exited(tool_pid)
                assert await process.wait() == 128 + signal.SIGKILL
        finally:
            with suppress(ProcessLookupError):
                os.killpg(process.native_pid, signal.SIGKILL)
            await process.wait()
    finally:
        owner.close()


async def test_runner_sigkill_fences_an_active_native_group_before_successor_dispatch(
    harness: protocol_pb2.Harness,
    endpoint: AnthropicMessages | OpenAIResponses,
    model: ScriptedModel,
    spec: protocol_pb2.SessionSpec,
    start_runner: Callable[..., Awaitable[RunnerProcess]],
    tmp_path: Path,
) -> None:
    """A successor cannot dispatch while the killed runner's native harness awaits upstream."""
    first_runner = await start_runner()
    client = RunnerClient(first_runner.target, capture_history=True)
    first = await client.attach("writer-handoff-active", spec=spec)
    await first.send("active-before-crash", "Reply with exactly: ACTIVE_FENCE_OK")
    active_request = await model.request()
    assert active_request.user_texts[-1] == "Reply with exactly: ACTIVE_FENCE_OK"
    await model.hold(active_request)
    await first.until(events.is_kind("harness_user_message_confirmed"))
    harness_pids = [entry.event.harness_started.pid for entry in events.of_kind(first.seen, "harness_started")]

    contender = await asyncio.create_subprocess_exec(
        *launches.runner_command(harness, endpoint.origin, state_dir=tmp_path / "state"),
        env=_runner_environment(tmp_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    _, stderr = await contender.communicate()
    assert contender.returncode != 0
    assert "runner state directory" in stderr.decode()
    assert "already owned" in stderr.decode()
    assert model.request_count == 1

    # ``crash`` waits the actual native harness PID, not merely the dead runner or supervisor.
    await first_runner.crash(harness_pids)
    await client.close()

    replacement_runner = await start_runner()
    client = RunnerClient(replacement_runner.target, capture_history=True)
    recovered = await client.attach("writer-handoff-active", after_cursor=first.cursor)
    await recovered.until(events.is_kind("harness_lost"))
    await recovered.drain_until_end()

    # Once the old group is fenced, a replacement owns the same state volume and can dispatch.
    replacement = await client.attach("writer-handoff-successor", spec=spec)
    await replacement.send("successor-input", "Reply with exactly: SUCCESSOR_FENCE_OK")
    successor_request = await model.request()
    assert successor_request.user_texts[-1] == "Reply with exactly: SUCCESSOR_FENCE_OK"
    assert model.request_count == 2
    await model.reply(successor_request, Text("SUCCESSOR_FENCE_OK"))
    await replacement.until(events.turn_completed)
    await replacement.stop_runner_session("stop-after-active-fence")
    await replacement.drain_until_end()
    await client.close()


async def test_a_restarted_runner_reports_the_loss_and_resumes_the_conversation(
    start_runner: Callable[[], Awaitable[RunnerProcess]], model: ScriptedModel, spec: protocol_pb2.SessionSpec
) -> None:
    first_runner = await start_runner()
    client = RunnerClient(first_runner.target, capture_history=True)
    async with await client.attach("restart-1", spec=spec) as first:
        await first.send("input-1", "Reply with exactly: SEED_OK")
        request = await model.request()
        await model.reply(request, Text("SEED_OK"))
        await first.until(events.turn_completed)
    await client.close()
    harness_pids = [entry.event.harness_started.pid for entry in events.of_kind(first.seen, "harness_started")]
    await first_runner.crash(harness_pids)

    second_runner = await start_runner()
    client = RunnerClient(second_runner.target, capture_history=True)
    second = await client.attach("restart-1", spec=spec, after_cursor=first.cursor)
    lost = await second.until(events.is_kind("harness_lost"))
    started = await second.until(events.is_kind("harness_started"))
    assert lost.cursor < started.cursor
    assert started.event.harness_started.resumed
    assert not events.of_kind(second.seen, "turn_completed")
    await second.send("input-2", "Reply with exactly: RESUMED_OK")
    request = await model.request()
    assert request.user_texts == ["Reply with exactly: SEED_OK", "Reply with exactly: RESUMED_OK"]
    assert request.assistant_texts == ["SEED_OK"]
    await model.reply(request, Text("RESUMED_OK"))
    done = await second.until(events.turn_completed)
    assert done.event.turn_completed.status == event_pb2.TURN_STATUS_COMPLETED
    events.assert_contiguous([*first.seen, *second.seen])
    await second.stop_runner_session("stop-after-crash")
    await second.drain_until_end()
    await client.close()


async def test_crash_after_runner_receipt_before_native_dispatch_retries_once(
    start_runner: Callable[..., Awaitable[RunnerProcess]], model: ScriptedModel, spec: protocol_pb2.SessionSpec
) -> None:
    """This is intentionally a real runner/harness process test, not a mocked Session method.

    The checkpoint is after the committed ``dispatch_planned`` record and before either native
    adapter receives the command. Killing the runner there proves a fresh runner delivers the same
    command once, with its original id, rather than dropping it or guessing a terminal result.
    """
    command_id = "retry-after-receipt"
    first_runner = await start_runner(test_debug_checkpoint=("after-dispatch-planned", command_id))
    client = RunnerClient(first_runner.target, capture_history=True)
    first = await client.attach("restart-retry-1", spec=spec)
    # Both native harnesses make a durable resume point only after one completed turn. This is the
    # normal target for a later command and keeps the crash test about command recovery, not a
    # harness's first-thread bootstrap contract.
    await first.send("seed-before-retry", "Reply with exactly: RETRY_SEED_OK")
    seed_request = await model.request()
    await model.reply(seed_request, Text("RETRY_SEED_OK"))
    await first.until(events.turn_completed)
    await first.send(command_id, "Reply with exactly: RETRIED_ONCE_OK")
    await first.until(events.is_kind("command_admitted"))
    debug = await first.until(events.is_kind("debug_checkpoint"))
    assert (debug.event.debug_checkpoint.name, debug.event.debug_checkpoint.command_id) == (
        "after-dispatch-planned",
        command_id,
    )
    harness_pids = [entry.event.harness_started.pid for entry in events.of_kind(first.seen, "harness_started")]
    await first_runner.crash(harness_pids)
    await client.close()

    # The first process is dead; the successor sees the persisted checkpoint and issues the one
    # native command. The upstream has seen no request at the first checkpoint.
    second_runner = await start_runner()
    client = RunnerClient(second_runner.target, capture_history=True)
    second = await client.attach("restart-retry-1", spec=spec, after_cursor=first.cursor)
    request = await model.request()
    assert request.user_texts[-1] == "Reply with exactly: RETRIED_ONCE_OK"
    assert model.request_count == 2
    await model.reply(request, Text("RETRIED_ONCE_OK"))
    confirmed = await second.until(events.is_kind("harness_user_message_confirmed"))
    assert confirmed.event.harness_user_message_confirmed.origin_command_ids == [command_id]
    await second.until(events.turn_completed)
    await second.stop_runner_session("stop-after-retry")
    await second.drain_until_end()
    await client.close()


async def test_crash_after_terminal_effect_persists_replays_that_effect(
    start_runner: Callable[..., Awaitable[RunnerProcess]], model: ScriptedModel, spec: protocol_pb2.SessionSpec
) -> None:
    """A committed terminal effect replays exactly after process death, without receipt repair."""
    command_id = "replay-persisted-effect"
    # This case needs only the real harness/runner protocol path; let dispatch proceed to the
    # native confirmation gate, then crash after its atomic command/Event commit.
    first_runner = await start_runner(test_debug_checkpoint=("after-terminal-outcome", command_id))
    client = RunnerClient(first_runner.target, capture_history=True)
    first = await client.attach("restart-effect-1", spec=spec)
    await first.send("seed-before-effect", "Reply with exactly: EFFECT_SEED_OK")
    seed_request = await model.request()
    await model.reply(seed_request, Text("EFFECT_SEED_OK"))
    await first.until(events.turn_completed)
    await first.send(command_id, "Reply with exactly: EFFECT_REPLAY_OK")
    await first.until(events.is_kind("command_admitted"))
    # Claude's confirmation correlation is its first response-side `message_start`, unlike
    # Codex's turn-start acknowledgement. Feed the real upstream response before waiting for the
    # harness-neutral terminal-effect checkpoint: either harness may then reach the same durable
    # runner boundary without the test assuming a shared native ordering.
    target_request = await model.request()
    await model.reply(target_request, Text("EFFECT_REPLAY_OK"))
    debug = await first.until(events.is_kind("debug_checkpoint"))
    assert (debug.event.debug_checkpoint.name, debug.event.debug_checkpoint.command_id) == (
        "after-terminal-outcome",
        command_id,
    )
    [published] = [
        entry
        for entry in events.of_kind(first.seen, "harness_user_message_confirmed")
        if command_id in entry.event.harness_user_message_confirmed.origin_command_ids
    ]
    # Command outcome and Event already committed together, before the checkpoint.
    assert published.cursor < debug.cursor
    harness_pids = [entry.event.harness_started.pid for entry in events.of_kind(first.seen, "harness_started")]
    await first_runner.crash(harness_pids)
    await client.close()

    second_runner = await start_runner()
    client = RunnerClient(second_runner.target, capture_history=True)
    # Do not start a replacement harness here. This boundary proves the runner's own durable
    # event replay before a later continuation contract decides how to resume an interrupted native
    # turn; attaching without a spec is the protocol's diagnostic/replay path.
    second = await client.attach("restart-effect-1")
    confirmed = await second.until(lambda entry: entry.cursor == published.cursor)
    assert confirmed.SerializeToString() == published.SerializeToString()
    assert confirmed.event.harness_user_message_confirmed.text == "Reply with exactly: EFFECT_REPLAY_OK"
    assert confirmed.event.harness_user_message_confirmed.origin_command_ids == [command_id]
    await second.drain_until_end()
    assert (
        len(
            [
                entry
                for entry in events.of_kind(second.seen, "harness_user_message_confirmed")
                if command_id in entry.event.harness_user_message_confirmed.origin_command_ids
            ]
        )
        == 1
    )
    await client.close()


async def test_sigterm_stops_the_harness_cleanly_and_the_next_runner_resumes(
    start_runner: Callable[[], Awaitable[RunnerProcess]], model: ScriptedModel, spec: protocol_pb2.SessionSpec
) -> None:
    first_runner = await start_runner()
    client = RunnerClient(first_runner.target, capture_history=True)
    async with await client.attach("sigterm-1", spec=spec) as first:
        await first.send("input-1", "Reply with exactly: SEED_OK")
        await model.reply(await model.request(), Text("SEED_OK"))
        await first.until(events.turn_completed)
        (running,) = await client.list_sessions()
        assert running.session_id == "sigterm-1"
        assert running.harness_state == protocol_pb2.HARNESS_STATE_RUNNING
        assert running.spec == spec
        # Native frames after the turn's result may still be arriving.
        assert running.last_cursor >= first.cursor
    await client.close()
    # The runner exits only after the stop ladder has reaped every harness.
    await first_runner.stop()

    second_runner = await start_runner()
    client = RunnerClient(second_runner.target, capture_history=True)
    (stopped,) = await client.list_sessions()
    assert stopped.harness_state == protocol_pb2.HARNESS_STATE_STOPPED
    assert stopped.last_cursor > first.cursor
    second = await client.attach("sigterm-1", spec=spec, after_cursor=first.cursor)
    exited = await second.until(events.is_kind("harness_exited"))
    assert exited.event.harness_exited.stopped_by_runner
    started = await second.until(events.is_kind("harness_started"))
    assert started.event.harness_started.resumed
    assert not events.of_kind(second.seen, "harness_lost")
    await second.send("input-2", "Reply with exactly: RESUMED_OK")
    request = await model.request()
    assert request.user_texts == ["Reply with exactly: SEED_OK", "Reply with exactly: RESUMED_OK"]
    await model.reply(request, Text("RESUMED_OK"))
    done = await second.until(events.turn_completed)
    assert done.event.turn_completed.status == event_pb2.TURN_STATUS_COMPLETED
    events.assert_contiguous([*first.seen, *second.seen])
    await second.stop_runner_session("stop-after-sigterm")
    await second.drain_until_end()
    await client.close()


if __name__ == "__main__":
    pytest_bazel.main()
