"""The runner's `Runner` service, called through its control stream by a stand-in Runner Gateway:
typed command operations are admitted once and answered from the journal, refusals carry their status,
lifecycle calls reserve sessions and submitted work starts a harness in the background, and a lost
connection is replaced by a fresh one."""

import asyncio
import shlex
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import cast

import grpc
import pytest
import pytest_bazel
from google.protobuf.timestamp_pb2 import Timestamp
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.grpc.tunnel import local_server
from agentplane.proto import (
    command_pb2,
    event_log_pb2,
    event_pb2,
    runner_control_pb2,
    runner_control_pb2_grpc,
    runner_pb2,
)
from agentplane.runner.config import ClaudeLaunch, RunnerConfig
from agentplane.runner.control import RunnerControlClient, RunnerService
from agentplane.runner.harness_process import HarnessProcess
from agentplane.runner.model_config import ModelConfig
from agentplane.runner.reconnect import StreamLostError, StreamTiming
from agentplane.runner.runner import Runner, SealedSession
from agentplane.runner.session import Session
from agentplane.runner.store import IngressAcknowledgement, SessionRecord
from agentplane.runner.testing import launches
from agentplane.runner.testing.adapters import UnexpectedAdapter
from agentplane.runner.testing.driver import refusal
from agentplane.runner.testing.gateway import Gateway, serve_gateway

# gazelle:include_dep @pypi//protobuf

RUNNING = "test-running-session"
STOPPED = "test-stopped-session"
FAILED_SETUP = "test-failed-setup-session"
SPEC = runner_pb2.SessionSpec(harness=runner_pb2.HARNESS_CLAUDE, cwd="/test-workspace", model="test-model")


def stored(session_id: str) -> runner_pb2.SessionSpec:
    """`SPEC` as the runner stores it for `session_id`: its task behind the platform instructions."""
    spec = runner_pb2.SessionSpec()
    spec.CopyFrom(SPEC)
    spec.instructions = launches.SANDBOX_LAUNCH.instructions(session_id, SPEC.instructions)
    return spec


# Long liveness: a test gateway and runner share one event loop, so a stall on a loaded runner must not expire the stream.
TIMING = StreamTiming(heartbeat_s=0.1, liveness_s=5, backoff_initial_s=0.01, backoff_max_s=0.05)

type Service = runner_control_pb2_grpc.RunnerAsyncStub


class RecordingAdapter(UnexpectedAdapter):
    def __init__(self) -> None:
        self.submitted: list[str] = []
        self.interrupted: list[str] = []

    async def submit(self, command_id: str, text: str) -> None:
        self.submitted.append(command_id)

    async def interrupt(self, turn_id: str) -> None:
        self.interrupted.append(turn_id)

    async def change_model(self, command_id: str, model: str) -> None:
        return

    async def change_reasoning_effort(self, command_id: str, effort: str) -> None:
        return


class RunningProcess:
    def __init__(self) -> None:
        self.running = True
        self.native_pid = 123

    async def stop(self) -> None:
        self.running = False


@pytest.fixture
async def runner(tmp_path: Path) -> AsyncIterator[Runner]:
    async with Runner(
        RunnerConfig(
            state_dir=tmp_path / "state",
            launch=launches.SANDBOX_LAUNCH,
            model_config_resolver=launches.unconfigured_models(),
            # Never launched: `started` stands in for the harness.
            claude=ClaudeLaunch(
                binary=tmp_path / "test-unlaunched-harness",
                base_url="http://agentplane-test-endpoint.invalid",
                auth_token="test-token",
            ),
        )
    ) as runner:
        for session_id in (RUNNING, STOPPED):
            runner.store.write(session_id, SessionRecord.from_spec(stored(session_id)))
            # As a started runner holds every stored session.
            await runner._load(session_id)
        running = runner.sessions[RUNNING]
        running.adapter = RecordingAdapter()
        running.process = cast(HarnessProcess, RunningProcess())
        running.process_id = "test-process-running"
        running._process_state = runner_pb2.PROCESS_STATE_RUNNING
        running._ready = True
        runner.store.write(FAILED_SETUP, SessionRecord.from_spec(stored(FAILED_SETUP), setup_script="exit 1"))
        await (await runner._load(FAILED_SETUP)).emit(event_pb2.SetupFinished(exit_code=1), sources=[])
        yield runner


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The sessions whose harness the runner started, in order. Starting one marks it running."""
    started: list[str] = []

    async def start(session: Session) -> None:
        if not session.running:
            started.append(session.session_id)
            session.adapter, session.process = RecordingAdapter(), cast(HarnessProcess, RunningProcess())
            session.process_id = f"test-process-{session.session_id}"
            session._process_state = runner_pb2.PROCESS_STATE_RUNNING
            session._ready = True

    monkeypatch.setattr(Session, "ensure_running", start)
    return started


@pytest.fixture
async def gateway() -> AsyncIterator[Gateway]:
    async with serve_gateway() as served:
        yield served


@pytest.fixture
async def dial(runner: Runner, gateway: Gateway) -> AsyncIterator[Callable[[], Awaitable[Service]]]:
    """Has the runner dial `gateway`, once the test has scripted it, and returns its service there."""
    tasks: list[asyncio.Task[None]] = []

    async def start() -> Service:
        client = RunnerControlClient(
            RunnerService(runner),
            target=gateway.endpoint.target,
            bearer="test-placeholder",
            boot_id=runner.boot_id,
            timing=TIMING,
            adopt_incarnation=runner.adopt_incarnation,
            facts=runner.facts,
            facts_changed=runner.facts_changed,
        )
        tasks.append(asyncio.create_task(client.run()))
        return await gateway.control.runner()

    try:
        yield start
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def submit(session_id: str, command_id: str, text: str = "test input") -> runner_control_pb2.SubmitInputRequest:
    return runner_control_pb2.SubmitInputRequest(session_id=session_id, command_id=command_id, text=text)


def acknowledge_sealed_sessions(runner: Runner) -> None:
    """A test gateway's control stream does not run ingress, so acknowledge its final cursors explicitly."""
    for session_id in runner.store.session_ids():
        cursor = runner.sessions[session_id].journal.sealed_cursor
        assert cursor is not None
        runner.store.write_acknowledgement(session_id, IngressAcknowledgement(cursor=cursor, sealed=True))
    runner.acknowledged()


def open_session(
    session_id: str, spec: runner_pb2.SessionSpec = SPEC, setup_script: str | None = None
) -> runner_control_pb2.OpenSessionRequest:
    return runner_control_pb2.OpenSessionRequest(session_id=session_id, spec=spec, setup_script=setup_script)


async def wait_for_file(path: Path) -> None:
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(10), wait=wait_fixed(0.01), retry=retry_if_exception_type(AssertionError), reraise=True
    ):
        with attempt:
            assert await asyncio.to_thread(path.exists), path


async def test_a_redelivered_command_is_admitted_once_and_answered_from_the_journal(
    runner: Runner, gateway: Gateway, dial: Callable[[], Awaitable[Service]]
) -> None:
    service = await dial()
    first, second = await asyncio.gather(
        service.SubmitInput(submit(RUNNING, "test-input")), service.SubmitInput(submit(RUNNING, "test-input"))
    )
    assert first == second
    admitted = first.admission.event.command_admitted.command
    assert (admitted.command_id, admitted.submit_input.text) == ("test-input", "test input")
    assert (await runner.sessions[RUNNING].journal.admitted_entry("test-input")) == first.admission
    assert gateway.control.authorizations == ["Bearer test-placeholder"]


async def test_a_welcome_without_an_incarnation_uid_is_rejected_before_serving_calls(
    runner: Runner, gateway: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway.control.incarnation_uid = ""
    service = RunnerService(runner)
    client = RunnerControlClient(
        service,
        target=gateway.endpoint.target,
        bearer="test-placeholder",
        boot_id=runner.boot_id,
        timing=TIMING,
        adopt_incarnation=runner.adopt_incarnation,
        facts=runner.facts,
        facts_changed=runner.facts_changed,
    )

    async def serving_calls(*_: object) -> None:
        pytest.fail("the runner must reject Welcome before serving calls")

    monkeypatch.setattr(client, "serve", serving_calls)
    async with local_server(
        lambda server: runner_control_pb2_grpc.add_RunnerServicer_to_server(service, server)
    ) as runner_channel:
        with pytest.raises(StreamLostError, match="Welcome did not name the Sandbox incarnation UID"):
            await client.connect(runner_channel)

    assert runner.store.incarnation_uid is None


async def test_each_command_rpc_admits_its_command_and_a_command_id_belongs_to_one_operation(
    runner: Runner, dial: Callable[[], Awaitable[Service]]
) -> None:
    await runner.sessions[RUNNING].emit(event_pb2.TurnStarted(turn_id="test-turn"), sources=[])
    service = await dial()
    admissions: dict[str, event_log_pb2.EventEntry] = {
        "submit_input": (
            await service.SubmitInput(
                runner_control_pb2.SubmitInputRequest(session_id=RUNNING, command_id="test-submit", text="test input")
            )
        ).admission,
        "change_model": (
            await service.ChangeModel(
                runner_control_pb2.ChangeModelRequest(session_id=RUNNING, command_id="test-model", model="test-model")
            )
        ).admission,
        "change_reasoning_effort": (
            await service.ChangeReasoningEffort(
                runner_control_pb2.ChangeReasoningEffortRequest(
                    session_id=RUNNING, command_id="test-effort", effort="high"
                )
            )
        ).admission,
        "interrupt_turn": (
            await service.InterruptTurn(
                runner_control_pb2.InterruptTurnRequest(
                    session_id=RUNNING, command_id="test-interrupt", turn_id="test-turn"
                )
            )
        ).admission,
    }
    for operation, admission in admissions.items():
        assert admission.event.command_admitted.command.WhichOneof("operation") == operation
    redelivered = await service.SubmitInput(
        runner_control_pb2.SubmitInputRequest(session_id=RUNNING, command_id="test-submit", text="test input")
    )
    assert redelivered.admission == admissions["submit_input"]
    assert (
        await refusal(
            service.ChangeModel(
                runner_control_pb2.ChangeModelRequest(session_id=RUNNING, command_id="test-submit", model="test-model")
            )
        )
        == grpc.StatusCode.ALREADY_EXISTS
    )


@pytest.mark.parametrize("ending", ["stopped", "sealed"])
async def test_a_redelivery_after_the_harness_ended_is_answered_with_the_original_admission(
    ending: str, runner: Runner, dial: Callable[[], Awaitable[Service]], started: list[str]
) -> None:
    # The gateway settles a submission whose answer was lost only by delivering it again.
    service = await dial()
    first = await service.SubmitInput(submit(RUNNING, "test-input"))
    session = runner.sessions[RUNNING]
    if ending == "stopped":
        assert session.process is not None
        await session.process.stop()
    else:
        await service.Quiesce(runner_pb2.QuiesceRequest())
    assert not session.running
    assert await service.SubmitInput(submit(RUNNING, "test-input")) == first


async def test_refusals_carry_their_status(dial: Callable[[], Awaitable[Service]]) -> None:
    service = await dial()
    await service.SubmitInput(submit(RUNNING, "test-conflict", "first"))
    refused = [
        await refusal(service.SubmitInput(request))
        for request in [
            submit(RUNNING, "test-conflict", "different"),
            submit("test-unknown-session", "test-unknown"),
            runner_control_pb2.SubmitInputRequest(session_id=RUNNING, text="missing command ID"),
        ]
    ]
    assert refused == [grpc.StatusCode.ALREADY_EXISTS, grpc.StatusCode.NOT_FOUND, grpc.StatusCode.INVALID_ARGUMENT]


@pytest.mark.parametrize("ending", ["superseded", "silent"])
async def test_a_lost_connection_is_replaced_by_a_fresh_one(
    ending: str, gateway: Gateway, dial: Callable[[], Awaitable[Service]]
) -> None:
    if ending == "superseded":
        gateway.control.closes.append(grpc.StatusCode.ABORTED)
    else:
        gateway.control.silences = 1
    service = await dial()
    assert (await service.SubmitInput(submit(RUNNING, "test-after-reconnect"))).HasField("admission")
    hellos, peers = gateway.control.hellos, gateway.control.peers
    assert len(hellos) == 2
    # One runner process: the same boot ID, over a new HTTP/2 connection.
    assert hellos[0].runner_boot_id == hellos[1].runner_boot_id
    assert peers[0] != peers[1]


async def test_list_sessions_reports_every_session(dial: Callable[[], Awaitable[Service]]) -> None:
    listed = await (await dial()).ListSessions(runner_pb2.ListSessionsRequest())
    assert {(row.session_id, row.harness_state, row.setup_state) for row in listed.sessions} == {
        (RUNNING, runner_pb2.HARNESS_STATE_RUNNING, runner_pb2.SETUP_STATE_NOT_REQUIRED),
        (STOPPED, runner_pb2.HARNESS_STATE_STOPPED, runner_pb2.SETUP_STATE_NOT_REQUIRED),
        (FAILED_SETUP, runner_pb2.HARNESS_STATE_STOPPED, runner_pb2.SETUP_STATE_FAILED),
    }


async def test_open_creates_a_durable_reservation_without_starting_it(
    runner: Runner, dial: Callable[[], Awaitable[Service]], started: list[str]
) -> None:
    opened = (await (await dial()).OpenSession(open_session("test-new-session"))).attached
    assert (opened.session_id, opened.spec, opened.harness_state) == (
        "test-new-session",
        stored("test-new-session"),
        runner_pb2.HARNESS_STATE_STOPPED,
    )
    assert started == []
    assert runner.store.exists("test-new-session")


async def test_opening_an_existing_session_confirms_its_durable_reservation(
    dial: Callable[[], Awaitable[Service]], started: list[str]
) -> None:
    opened = (await (await dial()).OpenSession(open_session(STOPPED))).attached
    assert (opened.session_id, opened.harness_state) == (STOPPED, runner_pb2.HARNESS_STATE_STOPPED)
    assert started == []


@pytest.mark.parametrize(
    ("spec", "setup_script"),
    [
        (runner_pb2.SessionSpec(harness=SPEC.harness, cwd=SPEC.cwd, model="test-other-model"), None),
        (SPEC, "test other setup"),
    ],
)
async def test_opening_an_existing_session_with_another_spec_is_refused(
    spec: runner_pb2.SessionSpec, setup_script: str | None, dial: Callable[[], Awaitable[Service]], started: list[str]
) -> None:
    service = await dial()
    assert await refusal(service.OpenSession(open_session(STOPPED, spec, setup_script))) == (
        grpc.StatusCode.ALREADY_EXISTS
    )
    assert started == []


@pytest.mark.parametrize(("session_id", "code"), [("test-unknown-session", grpc.StatusCode.NOT_FOUND)])
async def test_submit_to_an_unknown_session_is_refused(
    session_id: str, code: grpc.StatusCode, dial: Callable[[], Awaitable[Service]]
) -> None:
    service = await dial()
    assert await refusal(service.SubmitInput(submit(session_id, "test-input"))) == code


async def test_submit_admits_before_one_shared_background_start_and_status_tracks_incarnation(
    runner: Runner, dial: Callable[[], Awaitable[Service]], monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    starts = 0

    async def held_start(session: Session) -> None:
        nonlocal starts
        starts += 1
        entered.set()
        await release.wait()
        session.adapter = RecordingAdapter()
        session.process = cast(HarnessProcess, RunningProcess())
        session.process_id = "test-process-stopped"
        session._process_state = runner_pb2.PROCESS_STATE_RUNNING
        session._ready = True

    monkeypatch.setattr(Session, "ensure_running", held_start)
    service = await dial()
    first, second = await asyncio.gather(
        service.SubmitInput(submit(STOPPED, "test-first")), service.SubmitInput(submit(STOPPED, "test-second"))
    )
    await entered.wait()
    session = runner.sessions[STOPPED]
    assert first.admission.event.HasField("command_admitted")
    assert second.admission.event.HasField("command_admitted")
    assert {command.command_id for command in await session.journal.pending_commands()} == {"test-first", "test-second"}
    assert starts == 1

    preparing = await service.GetStatus(runner_pb2.GetStatusRequest())
    assert preparing.runner_boot_id == runner.boot_id
    preparing_session = next(row for row in preparing.sessions if row.session_id == STOPPED)
    assert preparing_session.execution_state == runner_pb2.SESSION_EXECUTION_STATE_PREPARING
    assert preparing_session.busy
    assert not preparing_session.HasField("process_id")
    (existing_process,) = preparing.processes
    assert existing_process.assigned_session_ids == [RUNNING]
    assert existing_process.state == runner_pb2.PROCESS_STATE_RUNNING

    release.set()
    assert session._activation_task is not None
    await session._activation_task
    ready = await service.GetStatus(runner_pb2.GetStatusRequest())
    ready_session = next(row for row in ready.sessions if row.session_id == STOPPED)
    processes = {process.assigned_session_ids[0]: process for process in ready.processes}
    process = processes[STOPPED]
    assert ready.runner_boot_id == runner.boot_id
    assert ready_session.execution_state == runner_pb2.SESSION_EXECUTION_STATE_READY
    assert ready_session.process_id == process.process_id == "test-process-stopped"
    assert ready_session.busy
    assert process.HasField("pid")
    assert process.pid == 123
    assert process.assigned_session_ids == [STOPPED]


async def test_startup_failure_settles_admitted_work_and_status_returns_to_idle(
    runner: Runner, dial: Callable[[], Awaitable[Service]], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failed_start(_session: Session) -> None:
        raise RuntimeError("injected startup failure")

    monkeypatch.setattr(Session, "ensure_running", failed_start)
    service = await dial()
    response = await service.SubmitInput(submit(STOPPED, "startup-failure-input"))
    session = runner.sessions[STOPPED]
    assert response.admission.event.command_admitted.command.command_id == "startup-failure-input"
    assert session._activation_task is not None
    await session._activation_task
    for _ in range(3):
        if not session.busy:
            break
        await asyncio.sleep(0)

    status = await service.GetStatus(runner_pb2.GetStatusRequest())
    failed = next(row for row in status.sessions if row.session_id == STOPPED)
    assert failed.execution_state == runner_pb2.SESSION_EXECUTION_STATE_FAILED
    assert failed.failure_cursor is not None
    assert not failed.busy
    assert not failed.HasField("process_id")
    assert all(STOPPED not in process.assigned_session_ids for process in status.processes)
    assert await session.journal.pending_commands() == []


async def test_startup_failure_preserves_prior_commands_with_uncertain_native_effects(
    runner: Runner, dial: Callable[[], Awaitable[Service]], monkeypatch: pytest.MonkeyPatch
) -> None:
    session = runner.sessions[STOPPED]
    uncertain = command_pb2.Command(
        command_id="prior-dispatch", submit_input=command_pb2.SubmitInput(text="may already have reached the harness")
    )
    await session.journal.admit(uncertain)
    assert await session.journal.dispatch_planned(uncertain.command_id)
    session._pending_command_ids.add(uncertain.command_id)

    async def failed_start(_session: Session) -> None:
        raise RuntimeError("injected startup failure")

    monkeypatch.setattr(Session, "ensure_running", failed_start)
    service = await dial()
    response = await service.SubmitInput(submit(STOPPED, "new-before-dispatch"))
    assert response.admission.event.command_admitted.command.command_id == "new-before-dispatch"
    assert session._activation_task is not None
    await session._activation_task

    prior = await session.journal.get(uncertain.command_id)
    newly_admitted = await session.journal.get("new-before-dispatch")
    assert prior is not None
    assert prior.dispatch_planned
    assert prior.terminal_cursor is None
    assert newly_admitted is not None
    assert newly_admitted.terminal_cursor is not None
    assert [command.command_id for command in await session.journal.pending_commands()] == [uncertain.command_id]


async def test_stale_and_empty_interrupt_targets_are_rejected_before_admission(
    runner: Runner, dial: Callable[[], Awaitable[Service]]
) -> None:
    service = await dial()
    stale = runner_control_pb2.InterruptTurnRequest(session_id=RUNNING, command_id="stale", turn_id="old-turn")
    empty = runner_control_pb2.InterruptTurnRequest(session_id=RUNNING, command_id="empty", turn_id="")
    assert await refusal(service.InterruptTurn(stale)) == grpc.StatusCode.FAILED_PRECONDITION
    assert await refusal(service.InterruptTurn(empty)) == grpc.StatusCode.INVALID_ARGUMENT
    assert await runner.sessions[RUNNING].journal.pending_commands() == []
    assert await runner.sessions[RUNNING].journal.get("stale") is None
    assert await runner.sessions[RUNNING].journal.get("empty") is None


async def test_runner_restart_settles_an_interrupt_without_starting_a_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = RunnerConfig(state_dir=tmp_path / "state", launch=launches.SANDBOX_LAUNCH)
    session_id = "interrupt-only-recovery"
    command = command_pb2.Command(
        command_id="interrupt-before-restart", interrupt_turn=command_pb2.InterruptTurn(turn_id="turn-before-restart")
    )
    async with Runner(config) as first:
        await first.startup()
        first.store.write(session_id, SessionRecord.from_spec(stored(session_id)))
        session = await first._load(session_id)
        await session.emit(event_pb2.HarnessStarted(resumed=False, pid=123), sources=[])
        await session.emit(event_pb2.TurnStarted(turn_id="turn-before-restart"), sources=[])
        await session.journal.admit(command, expected_active_turn_id="turn-before-restart")
        assert await session.journal.dispatch_planned(command.command_id)
        session._pending_command_ids.add(command.command_id)
        original_admission = await session.journal.admitted_entry(command.command_id)
        await session.emit(
            event_pb2.TurnCompleted(turn_id="turn-before-restart", status=event_pb2.TURN_STATUS_COMPLETED), sources=[]
        )

    activation_attempts: list[str] = []

    def track_activation(session: Session) -> None:
        activation_attempts.append(session.session_id)

    monkeypatch.setattr(Session, "ensure_activation", track_activation)
    async with Runner(config) as restarted:
        await restarted.startup()
        session = restarted.sessions[session_id]
        await session.command(command)

        terminal = await session.journal.get(command.command_id)
        assert terminal is not None
        assert terminal.dispatch_planned
        assert terminal.terminal_cursor is not None
        assert await session.journal.pending_commands() == []
        assert (await session.journal.admitted_entry(command.command_id)).cursor == original_admission.cursor
        entries = await session.journal.since(0, limit=128)
        admissions = [
            entry
            for entry in entries
            if entry.event.HasField("command_admitted")
            and entry.event.command_admitted.command.command_id == command.command_id
        ]
        assert [entry.cursor for entry in admissions] == [original_admission.cursor]
        assert any(
            entry.event.HasField("command_noop") and entry.event.command_noop.command_id == command.command_id
            for entry in entries
        )
        assert not any(
            entry.event.HasField("harness_started") and entry.cursor > original_admission.cursor for entry in entries
        )
        assert activation_attempts == []


async def test_new_incarnation_recovers_admitted_work_only_after_reopening_its_seal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = RunnerConfig(state_dir=tmp_path / "state", launch=launches.SANDBOX_LAUNCH)
    session_id = "sealed-admission-recovery"
    command = command_pb2.Command(command_id="uncertain-input", submit_input=command_pb2.SubmitInput(text="retained"))
    async with Runner(config) as first:
        first.store.adopt_incarnation("old-incarnation")
        first.store.write(session_id, SessionRecord.from_spec(stored(session_id)))
        session = await first._load(session_id)
        await session.journal.admit(command)
        assert await session.journal.dispatch_planned(command.command_id)
        await first.quiesce()
        assert session.journal.sealed_cursor is not None

    requested_activation: list[str] = []

    def defer_activation(session: Session) -> None:
        requested_activation.append(session.session_id)

    monkeypatch.setattr(Session, "ensure_activation", defer_activation)
    async with Runner(config) as restarted:
        await restarted.startup()
        assert requested_activation == []
        assert restarted.sessions[session_id].journal.sealed_cursor is not None
        await restarted.adopt_incarnation("new-incarnation")
        assert requested_activation == [session_id]
        assert restarted.sessions[session_id].journal.sealed_cursor is None
        assert [item.command_id for item in await restarted.sessions[session_id].journal.pending_commands()] == [
            command.command_id
        ]
        # A control-stream reconnect must not repeat recovery or turn a live interrupt into a loss.
        await restarted.adopt_incarnation("new-incarnation")
        assert requested_activation == [session_id]


async def test_process_lost_turn_recovery_survives_restart_until_native_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = RunnerConfig(state_dir=tmp_path / "state", launch=launches.SANDBOX_LAUNCH)
    session_id = "process-lost-recovery"
    async with Runner(config) as first:
        await first.startup()
        first.store.write(session_id, SessionRecord.from_spec(stored(session_id)))
        session = await first._load(session_id)
        await session.emit(event_pb2.HarnessStarted(resumed=False, pid=123), sources=[])
        await session.emit(event_pb2.TurnStarted(turn_id="lost-turn"), sources=[])

    requested_activation: list[str] = []

    def defer_activation(session: Session) -> None:
        requested_activation.append(session.session_id)

    async with Runner(config) as restarted_before_harness_start:
        with monkeypatch.context() as patcher:
            patcher.setattr(Session, "ensure_activation", defer_activation)
            await restarted_before_harness_start.startup()
        session = restarted_before_harness_start.sessions[session_id]
        assert requested_activation == [session_id]
        entries = await session.journal.since(0, limit=128)
        process_lost = next(
            entry
            for entry in entries
            if entry.event.HasField("turn_completed")
            and entry.event.turn_completed.status == event_pb2.TURN_STATUS_PROCESS_LOST
        )
        placeholder = next(
            entry
            for entry in entries
            if entry.event.HasField("conversation_reconciled")
            and entry.event.conversation_reconciled.turn_id == "lost-turn"
        )
        assert process_lost.cursor < placeholder.cursor
        assert await session.journal.unreconciled_process_lost_turns() == ["lost-turn"]

    async def reconcile_with_empty_native_history(session: Session) -> None:
        session.process = cast(HarnessProcess, RunningProcess())
        session.process_id = "recovered-process"
        session._process_state = runner_pb2.PROCESS_STATE_RUNNING
        await session.emit(event_pb2.HarnessStarted(resumed=True, pid=123), sources=[])
        for turn_id in await session.journal.recovery_turns():
            await session.emit(event_pb2.ConversationReconciled(turn_id=turn_id), sources=[])
        session._ready = True

    async with Runner(config) as recovered:
        monkeypatch.setattr(Session, "ensure_running", reconcile_with_empty_native_history)
        await recovered.startup()
        session = recovered.sessions[session_id]
        assert session._activation_task is not None
        await session._activation_task
        assert await session.journal.unreconciled_process_lost_turns() == []


async def test_a_quiesce_waits_for_the_admission_under_way_and_a_delivery_after_it_is_refused(
    runner: Runner, gateway: Gateway, dial: Callable[[], Awaitable[Service]], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Holds the earlier command's admission until the Quiesce has arrived behind it.
    journal = runner.sessions[RUNNING].journal
    admitting, unblocked = asyncio.Event(), asyncio.Event()
    get = journal.get

    async def held_get(command_id: str) -> object:
        admitting.set()
        await unblocked.wait()
        return await get(command_id)

    monkeypatch.setattr(journal, "get", held_get)
    quiesce = runner.quiesce
    quiesces = 0

    async def counted_quiesce() -> list[SealedSession]:
        nonlocal quiesces
        quiesces += 1
        return await quiesce()

    monkeypatch.setattr(runner, "quiesce", counted_quiesce)
    service = await dial()
    async with asyncio.timeout(10):
        before = asyncio.ensure_future(service.SubmitInput(submit(RUNNING, "test-before-seal")))
        await admitting.wait()
        sealed = await service.Quiesce(runner_pb2.QuiesceRequest())
        assert int(sealed.stage) == runner_control_pb2.RunnerFacts.QUIESCE_STAGE_DRAINING
        await gateway.control.wait_for_facts(
            runner_control_pb2.RunnerFacts(quiesce_stage=runner_control_pb2.RunnerFacts.QUIESCE_STAGE_DRAINING)
        )
        repeated = await service.Quiesce(runner_pb2.QuiesceRequest())
        assert repeated.stage == sealed.stage
        after = asyncio.create_task(refusal(service.SubmitInput(submit(RUNNING, "test-after-seal"))))
        unblocked.set()
        admitted, refused = await asyncio.gather(before, after)
        await service.ListSessions(runner_pb2.ListSessionsRequest())
    assert runner.sessions[RUNNING].journal.sealed_cursor is not None
    seal_cursor = runner.sessions[RUNNING].journal.sealed_cursor
    assert seal_cursor is not None
    assert admitted.admission.cursor <= seal_cursor
    assert quiesces == 1
    assert refused == grpc.StatusCode.ABORTED


async def test_quiesce_does_not_wait_for_model_resolution_or_allow_a_late_session(
    runner: Runner, gateway: Gateway, dial: Callable[[], Awaitable[Service]], monkeypatch: pytest.MonkeyPatch
) -> None:
    resolving, continue_resolution = asyncio.Event(), asyncio.Event()
    resolve = runner.config.model_config_resolver.resolve

    async def held_resolve(*, base_url: str, token: str, model: str) -> ModelConfig | None:
        resolving.set()
        await continue_resolution.wait()
        return await resolve(base_url=base_url, token=token, model=model)

    monkeypatch.setattr(runner.config.model_config_resolver, "resolve", held_resolve)
    service = await dial()
    opening = asyncio.ensure_future(service.OpenSession(open_session("test-open-under-quiesce")))
    await resolving.wait()

    try:
        response = await service.Quiesce(runner_pb2.QuiesceRequest())
        assert int(response.stage) == runner_control_pb2.RunnerFacts.QUIESCE_STAGE_DRAINING
        assert not runner.store.exists("test-open-under-quiesce")
        await service.ListSessions(runner_pb2.ListSessionsRequest())
        await gateway.control.wait_for_facts(Facts(quiesce_stage=Facts.QUIESCE_STAGE_FLUSHING))
        assert all(session.journal.sealed_cursor is not None for session in runner.sessions.values())
        assert not runner.store.exists("test-open-under-quiesce")
    finally:
        # Deletion may finish while model lookup is still held. If it later returns, the final
        # check immediately before the durable record prevents a late journal. Always release the
        # gate so a failed assertion cannot strand the protected OpenSession task.
        continue_resolution.set()
        await asyncio.gather(opening, return_exceptions=True)
    assert await refusal(opening) == grpc.StatusCode.ABORTED
    assert not runner.store.exists("test-open-under-quiesce")


async def test_quiesce_waits_for_bootstrap_and_settles_admitted_work(
    runner: Runner, dial: Callable[[], Awaitable[Service]]
) -> None:
    state_dir = runner.config.state_dir
    started, continue_bootstrap, completed = (
        state_dir / name for name in ("bootstrap-started", "bootstrap-continue", "bootstrap-completed")
    )
    script = (
        f"printf started > {shlex.quote(str(started))}\n"
        f"while [ ! -f {shlex.quote(str(continue_bootstrap))} ]; do sleep 0.01; done\n"
        f"printf completed > {shlex.quote(str(completed))}\n"
    )
    runner.config = replace(runner.config, launch=replace(runner.config.launch, bootstrap=script))
    initialization: asyncio.Task[None] | None = None
    admitted: runner_control_pb2.SubmitInputResponse | None = None
    draining: asyncio.Future[runner_pb2.ListSessionsResponse] | None = None
    try:
        _, initialization = await runner.initialize(script)
        assert initialization is not None
        service = await dial()
        await service.OpenSession(open_session("test-open-under-bootstrap"))
        admitted = await service.SubmitInput(submit("test-open-under-bootstrap", "test-bootstrap-gated-input"))
        await wait_for_file(started)

        response = await service.Quiesce(runner_pb2.QuiesceRequest())
        assert int(response.stage) == runner_control_pb2.RunnerFacts.QUIESCE_STAGE_DRAINING
        draining = asyncio.ensure_future(service.ListSessions(runner_pb2.ListSessionsRequest()))
        await asyncio.sleep(0)
        assert not draining.done()
        assert not completed.exists()
        assert int(runner.facts().bootstrap) == Facts.BOOTSTRAP_RUNNING
    finally:
        continue_bootstrap.touch()
        if draining is not None:
            await asyncio.wait_for(draining, timeout=10)
    assert initialization is not None
    assert admitted is not None
    assert draining is not None
    assert draining.done()
    assert completed.exists()
    assert initialization.done()
    assert runner.store.exists("test-open-under-bootstrap")
    session = runner.sessions["test-open-under-bootstrap"]
    assert await session.journal.pending_commands() == []
    assert session.journal.sealed_cursor is not None
    entries = await session.journal.since(admitted.admission.cursor - 1, limit=16)
    assert any(entry.event.HasField("command_failed") for entry in entries)


async def test_empty_runner_can_reserve_before_bootstrap_finishes_and_quiesce_it(
    tmp_path: Path, gateway: Gateway
) -> None:
    started, continue_bootstrap, completed = (
        tmp_path / name for name in ("bootstrap-started", "bootstrap-continue", "bootstrap-completed")
    )
    script = (
        f"printf started > {shlex.quote(str(started))}\n"
        f"while [ ! -f {shlex.quote(str(continue_bootstrap))} ]; do sleep 0.01; done\n"
        f"printf completed > {shlex.quote(str(completed))}\n"
    )
    async with Runner(
        RunnerConfig(
            state_dir=tmp_path / "empty-runner",
            launch=replace(launches.SANDBOX_LAUNCH, bootstrap=script),
            model_config_resolver=launches.unconfigured_models(),
            claude=ClaudeLaunch(
                binary=tmp_path / "test-unlaunched-harness",
                base_url="http://agentplane-test-endpoint.invalid",
                auth_token="test-token",
            ),
        )
    ) as empty:
        await empty.startup()
        client = RunnerControlClient(
            RunnerService(empty),
            target=gateway.endpoint.target,
            bearer="test-placeholder",
            boot_id=empty.boot_id,
            timing=TIMING,
            adopt_incarnation=empty.adopt_incarnation,
            facts=empty.facts,
            facts_changed=empty.facts_changed,
        )
        connection = asyncio.create_task(client.run())
        opening: asyncio.Task[runner_control_pb2.OpenSessionResponse] | None = None
        draining: asyncio.Future[runner_pb2.ListSessionsResponse] | None = None
        try:
            service = await gateway.control.runner()
            opening = asyncio.ensure_future(service.OpenSession(open_session("test-empty-bootstrap")))
            await opening
            admitted = await service.SubmitInput(submit("test-empty-bootstrap", "test-empty-bootstrap-input"))
            await wait_for_file(started)

            response = await service.Quiesce(runner_pb2.QuiesceRequest())
            assert int(response.stage) == runner_control_pb2.RunnerFacts.QUIESCE_STAGE_DRAINING
            draining = asyncio.ensure_future(service.ListSessions(runner_pb2.ListSessionsRequest()))
            await asyncio.sleep(0)
            assert not draining.done()
            assert not completed.exists()
            assert int(empty.facts().bootstrap) == Facts.BOOTSTRAP_RUNNING
            continue_bootstrap.touch()
            await asyncio.wait_for(draining, timeout=10)
            await gateway.control.wait_for_facts(
                Facts(bootstrap=Facts.BOOTSTRAP_SUCCEEDED, quiesce_stage=Facts.QUIESCE_STAGE_FLUSHING)
            )
            acknowledge_sealed_sessions(empty)
            await gateway.control.wait_for_facts(
                Facts(bootstrap=Facts.BOOTSTRAP_SUCCEEDED, quiesce_stage=Facts.QUIESCE_STAGE_QUIESCED)
            )
            assert completed.exists()
            assert empty._initialization_task is not None
            assert empty._initialization_task.done()
            assert opening.done()
            assert empty.store.session_ids() == ["test-empty-bootstrap"]
            session = empty.sessions["test-empty-bootstrap"]
            assert session.journal.sealed_cursor is not None
            assert await session.journal.pending_commands() == []
            assert admitted.admission.cursor <= session.journal.sealed_cursor
        finally:
            continue_bootstrap.touch()
            if opening is not None:
                await asyncio.gather(opening, return_exceptions=True)
            if draining is not None:
                await asyncio.wait_for(asyncio.gather(draining, return_exceptions=True), timeout=10)
            connection.cancel()
            await asyncio.gather(connection, return_exceptions=True)
        assert opening is not None
        assert opening.done()


async def test_quiesce_seals_a_durable_open_before_its_setup_can_start(
    runner: Runner, gateway: Gateway, dial: Callable[[], Awaitable[Service]], monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "test-open-before-setup"
    load_started, continue_load = asyncio.Event(), asyncio.Event()
    load = runner._load
    first_load = True

    async def hold_open_load(candidate: str) -> Session:
        nonlocal first_load
        if candidate == session_id and first_load:
            first_load = False
            load_started.set()
            await continue_load.wait()
        return await load(candidate)

    monkeypatch.setattr(runner, "_load", hold_open_load)
    service = await dial()
    opening = asyncio.ensure_future(service.OpenSession(open_session(session_id, setup_script="exit 0")))
    try:
        await load_started.wait()
        assert runner.store.exists(session_id)
        response = await service.Quiesce(runner_pb2.QuiesceRequest())
        assert int(response.stage) == runner_control_pb2.RunnerFacts.QUIESCE_STAGE_DRAINING
        await service.ListSessions(runner_pb2.ListSessionsRequest())
        session = runner.sessions[session_id]
        assert session.journal.sealed_cursor is not None
    finally:
        # The durable record was included in the drain. Releasing OpenSession's load cannot start
        # setup on that already-sealed journal; also release it if an assertion fails.
        continue_load.set()
        await asyncio.gather(opening, return_exceptions=True)
    assert await refusal(opening) == grpc.StatusCode.ABORTED
    session = runner.sessions[session_id]
    assert session._setup_task is None


async def test_quiesce_waits_for_running_setup_to_finish(
    tmp_path: Path, runner: Runner, dial: Callable[[], Awaitable[Service]]
) -> None:
    started, continue_setup, completed = (
        tmp_path / name for name in ("setup-started", "setup-continue", "setup-completed")
    )
    script = (
        f"printf started > {shlex.quote(str(started))}\n"
        f"while [ ! -f {shlex.quote(str(continue_setup))} ]; do sleep 0.01; done\n"
        f"printf completed > {shlex.quote(str(completed))}\n"
    )
    session_id = "test-setup-under-quiesce"
    service = await dial()
    draining: asyncio.Future[runner_pb2.ListSessionsResponse] | None = None
    try:
        await service.OpenSession(open_session(session_id, setup_script=script))
        await wait_for_file(started)

        response = await service.Quiesce(runner_pb2.QuiesceRequest())
        assert int(response.stage) == runner_control_pb2.RunnerFacts.QUIESCE_STAGE_DRAINING
        draining = asyncio.ensure_future(service.ListSessions(runner_pb2.ListSessionsRequest()))
        await asyncio.sleep(0)
        assert not draining.done()
        assert not completed.exists()
        assert runner.sessions[session_id].setup_active
    finally:
        continue_setup.touch()
        if draining is not None:
            await asyncio.wait_for(draining, timeout=10)
    assert draining is not None
    assert draining.done()
    session = runner.sessions[session_id]
    assert completed.exists()
    assert session.setup_state == runner_pb2.SETUP_STATE_SUCCEEDED
    assert not session.setup_active
    assert session.journal.sealed_cursor is not None
    entries = await session.journal.since(0, limit=32)
    assert any(
        entry.event.HasField("setup_finished") and entry.event.setup_finished.exit_code == 0 for entry in entries
    )


async def test_quiesce_settles_admitted_work_when_it_cancels_background_startup(
    runner: Runner, dial: Callable[[], Awaitable[Service]], started: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    starting, continue_start = asyncio.Event(), asyncio.Event()
    start = Session.ensure_running

    async def held_start(session: Session) -> None:
        starting.set()
        await continue_start.wait()
        await start(session)

    monkeypatch.setattr(Session, "ensure_running", held_start)
    quiesce = runner.quiesce
    quiesces = 0

    async def counted_quiesce() -> list[SealedSession]:
        nonlocal quiesces
        quiesces += 1
        return await quiesce()

    monkeypatch.setattr(runner, "quiesce", counted_quiesce)
    service = await dial()
    admission = await service.SubmitInput(submit(STOPPED, "test-quiesce-pending"))
    try:
        await starting.wait()

        response = await service.Quiesce(runner_pb2.QuiesceRequest())
        assert int(response.stage) == runner_control_pb2.RunnerFacts.QUIESCE_STAGE_DRAINING
        await service.ListSessions(runner_pb2.ListSessionsRequest())
        assert quiesces == 1
        sealed_cursor = runner.sessions[STOPPED].journal.sealed_cursor
        assert sealed_cursor is not None
        assert not runner.sessions[STOPPED].running
        assert started == []
        assert await runner.sessions[STOPPED].journal.pending_commands() == []
        assert admission.admission.cursor < sealed_cursor
    finally:
        continue_start.set()


async def test_quiescing_again_does_not_repeat_the_drain_and_opening_is_refused(
    runner: Runner, dial: Callable[[], Awaitable[Service]], started: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    quiesce = runner.quiesce
    quiesces = 0

    async def counted_quiesce() -> list[SealedSession]:
        nonlocal quiesces
        quiesces += 1
        return await quiesce()

    monkeypatch.setattr(runner, "quiesce", counted_quiesce)
    service = await dial()
    await service.Quiesce(runner_pb2.QuiesceRequest())
    await service.Quiesce(runner_pb2.QuiesceRequest())
    await service.ListSessions(runner_pb2.ListSessionsRequest())
    assert quiesces == 1
    assert await refusal(service.OpenSession(open_session("test-new-session"))) == grpc.StatusCode.ABORTED
    assert not runner.sessions[RUNNING].running
    assert started == []


async def test_a_conditional_quiesce_enters_lameduck_only_while_idle_since_the_token(
    runner: Runner, gateway: Gateway, dial: Callable[[], Awaitable[Service]]
) -> None:
    service = await dial()
    await gateway.control.wait_for_idleness(idle=True)
    token = runner.facts().idle_since
    stale = Timestamp(seconds=token.seconds - 1, nanos=token.nanos)
    with runner.delivering():
        assert await refusal(service.Quiesce(runner_pb2.QuiesceRequest(if_idle_since=token))) == (
            grpc.StatusCode.ABORTED
        )
    assert await refusal(service.Quiesce(runner_pb2.QuiesceRequest(if_idle_since=stale))) == grpc.StatusCode.ABORTED
    assert not runner.lameduck
    # The delivery ended, so the Sandbox went idle again, later than `token`.
    assert await refusal(service.Quiesce(runner_pb2.QuiesceRequest(if_idle_since=token))) == grpc.StatusCode.ABORTED
    await service.Quiesce(runner_pb2.QuiesceRequest(if_idle_since=runner.facts().idle_since))
    assert runner.lameduck
    await service.Quiesce(runner_pb2.QuiesceRequest(if_idle_since=stale))


Facts = runner_control_pb2.RunnerFacts


async def test_the_first_heartbeat_of_a_connection_carries_the_facts(
    runner: Runner, gateway: Gateway, dial: Callable[[], Awaitable[Service]]
) -> None:
    await dial()
    await gateway.control.wait_for_facts(Facts())
    assert len(gateway.control.facts) == 1


async def test_quiescing_publishes_flushing_until_the_ingress_acknowledged_every_journal(
    runner: Runner, gateway: Gateway, dial: Callable[[], Awaitable[Service]], monkeypatch: pytest.MonkeyPatch
) -> None:
    service = await dial()
    flushed = runner._flushed
    monkeypatch.setattr(runner, "_flushed", lambda _session_id, _cursor: False)
    initial = await service.Quiesce(runner_pb2.QuiesceRequest())
    assert int(initial.stage) == runner_control_pb2.RunnerFacts.QUIESCE_STAGE_DRAINING
    await service.ListSessions(runner_pb2.ListSessionsRequest())
    quiesced = await service.Quiesce(runner_pb2.QuiesceRequest())
    assert int(quiesced.stage) == runner_control_pb2.RunnerFacts.QUIESCE_STAGE_FLUSHING
    await gateway.control.wait_for_facts(Facts(quiesce_stage=Facts.QUIESCE_STAGE_FLUSHING))
    for session_id in runner.store.session_ids():
        cursor = runner.sessions[session_id].journal.sealed_cursor
        assert cursor is not None
        runner.store.write_acknowledgement(session_id, IngressAcknowledgement(cursor=cursor, sealed=True))
    monkeypatch.setattr(runner, "_flushed", flushed)
    runner.acknowledged()
    await gateway.control.wait_for_facts(Facts(quiesce_stage=Facts.QUIESCE_STAGE_QUIESCED))


async def test_a_failed_quiesce_drain_retries_on_the_next_request(
    runner: Runner, gateway: Gateway, dial: Callable[[], Awaitable[Service]], monkeypatch: pytest.MonkeyPatch
) -> None:
    quiesce = runner.quiesce
    failed = asyncio.Event()
    quiesces = 0

    async def fail_once() -> list[SealedSession]:
        nonlocal quiesces
        quiesces += 1
        if quiesces == 1:
            failed.set()
            raise RuntimeError("test quiesce drain failure")
        return await quiesce()

    monkeypatch.setattr(runner, "quiesce", fail_once)
    service = await dial()
    first = await service.Quiesce(runner_pb2.QuiesceRequest())
    assert int(first.stage) == runner_control_pb2.RunnerFacts.QUIESCE_STAGE_DRAINING
    await failed.wait()
    await service.ListSessions(runner_pb2.ListSessionsRequest())

    retry = await service.Quiesce(runner_pb2.QuiesceRequest())
    assert int(retry.stage) == runner_control_pb2.RunnerFacts.QUIESCE_STAGE_DRAINING
    await service.ListSessions(runner_pb2.ListSessionsRequest())
    assert quiesces == 2
    acknowledge_sealed_sessions(runner)
    await gateway.control.wait_for_facts(Facts(quiesce_stage=Facts.QUIESCE_STAGE_QUIESCED))


async def test_quiesce_drain_survives_control_cancellation_and_reconnect(
    runner: Runner, gateway: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = RunnerService(runner)
    quiesce = runner.quiesce
    draining, finish_drain = asyncio.Event(), asyncio.Event()
    quiesces = 0

    async def held_quiesce() -> list[SealedSession]:
        nonlocal quiesces
        quiesces += 1
        draining.set()
        await finish_drain.wait()
        return await quiesce()

    monkeypatch.setattr(runner, "quiesce", held_quiesce)

    def control_task() -> asyncio.Task[None]:
        client = RunnerControlClient(
            service,
            target=gateway.endpoint.target,
            bearer=gateway.endpoint.bearer,
            boot_id=runner.boot_id,
            timing=TIMING,
            adopt_incarnation=runner.adopt_incarnation,
            facts=runner.facts,
            facts_changed=runner.facts_changed,
        )
        return asyncio.create_task(client.run())

    first_task, second_task = control_task(), None
    try:
        await gateway.control.wait_connected(1)
        first_stub = await gateway.control.runner()
        first = await first_stub.Quiesce(runner_pb2.QuiesceRequest())
        assert int(first.stage) == Facts.QUIESCE_STAGE_DRAINING
        await draining.wait()

        facts_before_reconnect = len(gateway.control.facts)
        first_task.cancel()
        await asyncio.gather(first_task, return_exceptions=True)
        second_task = control_task()
        await gateway.control.wait_connected(2)
        async with asyncio.timeout(10), gateway.control.facts_published:
            await gateway.control.facts_published.wait_for(
                lambda: any(
                    facts.quiesce_stage == Facts.QUIESCE_STAGE_DRAINING
                    for facts in gateway.control.facts[facts_before_reconnect:]
                )
            )

        second_stub = await gateway.control.runner()
        repeated = await second_stub.Quiesce(runner_pb2.QuiesceRequest())
        assert int(repeated.stage) == Facts.QUIESCE_STAGE_DRAINING
        assert quiesces == 1
        finish_drain.set()
        await second_stub.ListSessions(runner_pb2.ListSessionsRequest())
        acknowledge_sealed_sessions(runner)
        await gateway.control.wait_for_facts(Facts(quiesce_stage=Facts.QUIESCE_STAGE_QUIESCED))
        assert quiesces == 1
    finally:
        finish_drain.set()
        if second_task is not None:
            second_task.cancel()
        first_task.cancel()
        await asyncio.gather(first_task, *(task for task in (second_task,) if task is not None), return_exceptions=True)


async def test_a_delivery_under_way_makes_the_sandbox_busy_until_it_is_answered(
    runner: Runner, gateway: Gateway, dial: Callable[[], Awaitable[Service]]
) -> None:
    await dial()
    await gateway.control.wait_for_idleness(idle=True)
    idle_since = runner.facts().idle_since
    with runner.delivering():
        await gateway.control.wait_for_idleness(idle=False)
    await gateway.control.wait_for_idleness(idle=True)
    assert runner.facts().idle_since.ToNanoseconds() > idle_since.ToNanoseconds()


async def test_a_running_session_setup_is_published_even_while_the_sandbox_was_already_busy(
    runner: Runner, gateway: Gateway, dial: Callable[[], Awaitable[Service]]
) -> None:
    await dial()
    await gateway.control.wait_for_facts(Facts())
    session = runner.sessions[RUNNING]
    with runner.delivering():
        await gateway.control.wait_for_idleness(idle=False)
        session.setup_state = runner_pb2.SETUP_STATE_RUNNING
        runner.note_activity()
        await gateway.control.wait_for_facts(Facts(setups_running=True))
        session.setup_state = runner_pb2.SETUP_STATE_SUCCEEDED
        runner.note_activity()
    assert not runner.facts().setups_running


@pytest.mark.parametrize(
    ("script", "bootstrap"), [("exit 0", Facts.BOOTSTRAP_SUCCEEDED), ("exit 3", Facts.BOOTSTRAP_FAILED)]
)
async def test_the_bootstrap_result_is_published(
    script: str, bootstrap: Facts.Bootstrap, runner: Runner, gateway: Gateway, dial: Callable[[], Awaitable[Service]]
) -> None:
    await dial()
    _, execution = await runner.initialize(script)
    assert execution is not None
    await execution
    await gateway.control.wait_for_facts(Facts(bootstrap=bootstrap))


if __name__ == "__main__":
    pytest_bazel.main()
