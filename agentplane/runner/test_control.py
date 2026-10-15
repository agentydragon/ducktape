"""The runner's `Runner` service, called through its control stream by a stand-in Runner Gateway:
delivered commands are admitted once and answered from the journal, refusals carry their status,
lifecycle calls start a harness only when they say so, and a lost connection is replaced by a fresh
one."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import cast

import grpc
import pytest
import pytest_bazel
from google.protobuf.timestamp_pb2 import Timestamp

from agentplane.grpc_tunnel import local_server
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
from agentplane.runner.reconnect import StreamLostError, StreamTiming
from agentplane.runner.runner import Runner
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

    async def submit(self, command_id: str, text: str) -> None:
        self.submitted.append(command_id)


class RunningProcess:
    def __init__(self) -> None:
        self.running = True

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


def deliver(session_id: str, command: command_pb2.Command) -> runner_control_pb2.DeliverCommandRequest:
    return runner_control_pb2.DeliverCommandRequest(session_id=session_id, command=command)


def submit(command_id: str, text: str = "test input") -> command_pb2.Command:
    return command_pb2.Command(command_id=command_id, submit_input=command_pb2.SubmitInput(text=text))


def open_session(
    session_id: str, spec: runner_pb2.SessionSpec = SPEC, setup_script: str | None = None
) -> runner_control_pb2.OpenSessionRequest:
    return runner_control_pb2.OpenSessionRequest(session_id=session_id, spec=spec, setup_script=setup_script)


async def test_a_redelivered_command_is_admitted_once_and_answered_from_the_journal(
    runner: Runner, gateway: Gateway, dial: Callable[[], Awaitable[Service]]
) -> None:
    service = await dial()
    first, second = await asyncio.gather(
        service.DeliverCommand(deliver(RUNNING, submit("test-input"))),
        service.DeliverCommand(deliver(RUNNING, submit("test-input"))),
    )
    assert first == second
    assert first.admission.event.command_admitted.command == submit("test-input")
    assert (await runner.sessions[RUNNING].journal.admitted_entry("test-input")) == first.admission
    assert gateway.control.authorizations == ["Bearer test-placeholder"]


async def test_a_welcome_without_an_incarnation_uid_is_rejected_before_serving_calls(
    runner: Runner, gateway: Gateway
) -> None:
    gateway.control.incarnation_uid = ""
    service = RunnerService(runner)
    client = RunnerControlClient(
        service,
        target=gateway.endpoint.target,
        bearer="test-placeholder",
        timing=TIMING,
        adopt_incarnation=runner.adopt_incarnation,
        facts=runner.facts,
        facts_changed=runner.facts_changed,
    )
    async with local_server(
        lambda server: runner_control_pb2_grpc.add_RunnerServicer_to_server(service, server)
    ) as runner_channel:
        with pytest.raises(StreamLostError, match="Welcome did not name the Sandbox incarnation UID"):
            await client.connect(runner_channel)

    assert runner.store.incarnation_uid is None
    assert gateway.control.connections == 0


async def test_each_command_rpc_admits_its_command_and_a_command_id_belongs_to_one_operation(
    dial: Callable[[], Awaitable[Service]],
) -> None:
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
        "stop_runner_session": (
            await service.StopRunnerSession(
                runner_control_pb2.StopRunnerSessionRequest(session_id=RUNNING, command_id="test-stop")
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
    ending: str, runner: Runner, dial: Callable[[], Awaitable[Service]]
) -> None:
    # The gateway settles a submission whose answer was lost only by delivering it again.
    service = await dial()
    first = await service.DeliverCommand(deliver(RUNNING, submit("test-input")))
    session = runner.sessions[RUNNING]
    if ending == "stopped":
        assert session.process is not None
        await session.process.stop()
    else:
        await service.Quiesce(runner_pb2.QuiesceRequest())
    assert not session.running
    assert await service.DeliverCommand(deliver(RUNNING, submit("test-input"))) == first


async def test_refusals_carry_their_status(dial: Callable[[], Awaitable[Service]]) -> None:
    service = await dial()
    await service.DeliverCommand(deliver(RUNNING, submit("test-conflict", "first")))
    refused = [
        await refusal(service.DeliverCommand(deliver(session_id, command)))
        for session_id, command in [
            (RUNNING, submit("test-conflict", "different")),
            (STOPPED, submit("test-stopped")),
            ("test-unknown-session", submit("test-unknown")),
            (RUNNING, command_pb2.Command(command_id="test-invalid")),
        ]
    ]
    assert refused == [
        grpc.StatusCode.ALREADY_EXISTS,
        grpc.StatusCode.FAILED_PRECONDITION,
        grpc.StatusCode.NOT_FOUND,
        grpc.StatusCode.INVALID_ARGUMENT,
    ]


@pytest.mark.parametrize("ending", ["superseded", "silent"])
async def test_a_lost_connection_is_replaced_by_a_fresh_one(
    ending: str, gateway: Gateway, dial: Callable[[], Awaitable[Service]]
) -> None:
    if ending == "superseded":
        gateway.control.closes.append(grpc.StatusCode.ABORTED)
    else:
        gateway.control.silences = 1
    service = await dial()
    assert (await service.DeliverCommand(deliver(RUNNING, submit("test-after-reconnect")))).HasField("admission")
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


async def test_open_creates_a_session_and_starts_it(
    runner: Runner, dial: Callable[[], Awaitable[Service]], started: list[str]
) -> None:
    opened = (await (await dial()).OpenSession(open_session("test-new-session"))).attached
    assert (opened.session_id, opened.spec, opened.harness_state) == (
        "test-new-session",
        stored("test-new-session"),
        runner_pb2.HARNESS_STATE_RUNNING,
    )
    assert started == ["test-new-session"]
    assert runner.store.exists("test-new-session")


async def test_opening_an_existing_session_confirms_it_and_starts_its_stopped_harness(
    dial: Callable[[], Awaitable[Service]], started: list[str]
) -> None:
    opened = (await (await dial()).OpenSession(open_session(STOPPED))).attached
    assert (opened.session_id, opened.harness_state) == (STOPPED, runner_pb2.HARNESS_STATE_RUNNING)
    assert started == [STOPPED]


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


async def test_resume_restarts_a_stopped_harness_on_its_retained_spec(
    dial: Callable[[], Awaitable[Service]], started: list[str]
) -> None:
    resumed = await (await dial()).ResumeSession(runner_control_pb2.ResumeSessionRequest(session_id=STOPPED))
    assert (resumed.attached.spec, resumed.attached.harness_state) == (
        stored(STOPPED),
        runner_pb2.HARNESS_STATE_RUNNING,
    )
    assert started == [STOPPED]


@pytest.mark.parametrize(
    ("session_id", "code"),
    [(FAILED_SETUP, grpc.StatusCode.FAILED_PRECONDITION), ("test-unknown-session", grpc.StatusCode.NOT_FOUND)],
)
async def test_a_resume_the_runner_cannot_serve_is_refused(
    session_id: str, code: grpc.StatusCode, dial: Callable[[], Awaitable[Service]], started: list[str]
) -> None:
    service = await dial()
    assert await refusal(service.ResumeSession(runner_control_pb2.ResumeSessionRequest(session_id=session_id))) == code
    assert started == []


async def test_a_quiesce_waits_for_the_admission_under_way_and_a_delivery_after_it_is_refused(
    runner: Runner, dial: Callable[[], Awaitable[Service]], monkeypatch: pytest.MonkeyPatch
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
    seal_arrived = asyncio.Event()
    quiesce = RunnerService.Quiesce

    async def observed_quiesce(
        service: RunnerService, request: runner_pb2.QuiesceRequest, context: grpc.aio.ServicerContext
    ) -> runner_pb2.QuiesceResponse:
        seal_arrived.set()
        return await quiesce(service, request, context)

    monkeypatch.setattr(RunnerService, "Quiesce", observed_quiesce)
    service = await dial()
    async with asyncio.timeout(10):
        before = asyncio.ensure_future(service.DeliverCommand(deliver(RUNNING, submit("test-before-seal"))))
        await admitting.wait()
        sealing = asyncio.ensure_future(service.Quiesce(runner_pb2.QuiesceRequest()))
        await seal_arrived.wait()
        after = asyncio.create_task(refusal(service.DeliverCommand(deliver(RUNNING, submit("test-after-seal")))))
        unblocked.set()
        admitted, sealed, refused = await asyncio.gather(before, sealing, after)
    assert admitted.admission.cursor <= {row.session_id: row.cursor for row in sealed.sessions}[RUNNING]
    assert refused == grpc.StatusCode.ABORTED


async def test_quiescing_again_returns_the_same_cursors_and_opening_is_refused(
    runner: Runner, dial: Callable[[], Awaitable[Service]], started: list[str]
) -> None:
    service = await dial()
    first, second = (
        await service.Quiesce(runner_pb2.QuiesceRequest()),
        await service.Quiesce(runner_pb2.QuiesceRequest()),
    )
    assert first == second
    assert {row.session_id for row in first.sessions} == {RUNNING, STOPPED, FAILED_SETUP}
    assert await refusal(service.OpenSession(open_session("test-new-session"))) == grpc.StatusCode.ABORTED
    assert await refusal(service.ResumeSession(runner_control_pb2.ResumeSessionRequest(session_id=STOPPED))) == (
        grpc.StatusCode.ABORTED
    )
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
    runner: Runner, gateway: Gateway, dial: Callable[[], Awaitable[Service]]
) -> None:
    service = await dial()
    quiesced = await service.Quiesce(runner_pb2.QuiesceRequest())
    await gateway.control.wait_for_facts(Facts(quiesce_stage=Facts.QUIESCE_STAGE_FLUSHING))
    for row in quiesced.sessions:
        runner.store.write_acknowledgement(row.session_id, IngressAcknowledgement(cursor=row.cursor, sealed=True))
    runner.acknowledged()
    await gateway.control.wait_for_facts(Facts(quiesce_stage=Facts.QUIESCE_STAGE_QUIESCED))


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
