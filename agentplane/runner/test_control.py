"""The runner's control-stream peer against a scripted Sandbox Service: delivered commands are admitted once
and answered from the journal, refusals name their cause, lifecycle requests start a harness only when
they say so, and a lost connection is replaced by a fresh one."""

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast, override

import grpc
import httpx
import pytest
import pytest_bazel

from agentplane.grpc_serving import serving
from agentplane.proto import (
    command_pb2,
    event_pb2,
    runner_control_pb2 as control_pb2,
    runner_control_pb2_grpc,
    runner_pb2,
)
from agentplane.runner.adapter import HarnessAdapter
from agentplane.runner.config import ClaudeLaunch, RunnerConfig
from agentplane.runner.control import ControlTiming, RunnerControlClient
from agentplane.runner.harness_process import HarnessProcess
from agentplane.runner.model_config import HttpModelConfigResolver
from agentplane.runner.service import Runner
from agentplane.runner.session import Session
from agentplane.runner.store import SessionRecord

# gazelle:include_dep @pypi//protobuf

RUNNING = "test-running-session"
STOPPED = "test-stopped-session"
FAILED_SETUP = "test-failed-setup-session"
SPEC = runner_pb2.SessionSpec(harness=runner_pb2.HARNESS_CLAUDE, cwd="/test-workspace", model="test-model")
TIMING = ControlTiming(heartbeat_s=0.1, liveness_s=0.5, backoff_initial_s=0.01, backoff_max_s=0.05)
Reason = control_pb2.CommandRefused
Refused = control_pb2.ControlRefused


class RecordingAdapter(HarnessAdapter):
    def __init__(self) -> None:
        self.submitted: list[str] = []

    def command(self) -> list[str]:
        return []

    def environment(self) -> Mapping[str, str]:
        return {}

    async def handshake(self) -> str:
        return "test-native-session"

    async def reconcile(self, turn_id: str, *, resumed: bool) -> event_pb2.ConversationReconciled:
        return event_pb2.ConversationReconciled(turn_id=turn_id)

    async def submit(self, command_id: str, text: str) -> None:
        self.submitted.append(command_id)

    async def interrupt(self, turn_id: str) -> None:
        raise AssertionError(f"unexpected interrupt of {turn_id}")

    async def change_model(self, command_id: str, model: str) -> None:
        raise AssertionError(f"unexpected model change {command_id}")

    async def change_reasoning_effort(self, command_id: str, effort: str) -> None:
        raise AssertionError(f"unexpected effort change {command_id}")

    async def on_frame(self, frame: dict[str, Any], source_sequence: int) -> None:
        raise AssertionError(f"unexpected native frame {source_sequence}")


class RunningProcess:
    def __init__(self) -> None:
        self.running = True

    async def stop(self) -> None:
        self.running = False


@dataclass
class ScriptedService(runner_control_pb2_grpc.RunnerControlServicer):
    """Each connection gets Welcome, then its scripted frames, or the scripted status instead, then
    what the test pushes to it."""

    frames: list[control_pb2.ServiceFrame] = field(default_factory=list)
    # Ends the next connections right after Welcome, oldest first.
    closes: list[grpc.StatusCode] = field(default_factory=list)
    # Sends nothing after Welcome on the next connections, oldest first.
    silences: int = 0
    hellos: list[control_pb2.Hello] = field(default_factory=list)
    peers: list[str] = field(default_factory=list)
    authorizations: list[str] = field(default_factory=list)
    receipts: asyncio.Queue[control_pb2.CommandReceipt] = field(default_factory=asyncio.Queue)
    control_results: asyncio.Queue[control_pb2.ControlResult] = field(default_factory=asyncio.Queue)
    capabilities: list[str] = field(default_factory=lambda: ["commands", "lifecycle"])
    # Each connection's send queue, holding its scripted frames, for the test to push more to.
    connected: asyncio.Queue[asyncio.Queue[control_pb2.ServiceFrame | grpc.StatusCode]] = field(
        default_factory=asyncio.Queue
    )
    dial: Callable[[], None] = lambda: None

    @override
    async def Connect(  # type: ignore[override]
        self, request_iterator: AsyncIterator[control_pb2.RunnerFrame], context: grpc.aio.ServicerContext
    ) -> None:
        self.authorizations += [
            str(value) for key, value in context.invocation_metadata() or () if key == "authorization"
        ]
        self.hellos.append((await anext(request_iterator)).hello)
        self.peers.append(context.peer())
        await context.write(
            control_pb2.ServiceFrame(
                welcome=control_pb2.Welcome(connection_epoch=len(self.hellos), capabilities=self.capabilities)
            )
        )
        if self.closes:
            await context.abort(self.closes.pop(0), "test service closes this connection")
        if self.silences:
            self.silences -= 1
            await asyncio.Event().wait()
        # The frames to send on this connection, or the status that ends it.
        pushed: asyncio.Queue[control_pb2.ServiceFrame | grpc.StatusCode] = asyncio.Queue()
        for scripted in self.frames:
            pushed.put_nowait(scripted)
        await self.connected.put(pushed)

        async def receive() -> None:
            async for frame in request_iterator:
                if frame.HasField("receipt"):
                    await self.receipts.put(frame.receipt)
                if frame.HasField("control_result"):
                    await self.control_results.put(frame.control_result)
                await pushed.put(control_pb2.ServiceFrame(heartbeat=control_pb2.Heartbeat()))

        receiving = asyncio.create_task(receive())
        try:
            while not isinstance(item := await pushed.get(), grpc.StatusCode):
                await context.write(item)
            await context.abort(item, "test service ends this connection")
        finally:
            receiving.cancel()


@pytest.fixture
async def runner(tmp_path: Path) -> AsyncIterator[Runner]:
    runner = Runner(
        RunnerConfig(
            state_dir=tmp_path / "state",
            model_config_resolver=HttpModelConfigResolver(
                transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=b"null"))
            ),
            # Never launched: `started` stands in for the harness.
            claude=ClaudeLaunch(
                binary=tmp_path / "test-unlaunched-harness",
                base_url="http://agentplane-test-endpoint.invalid",
                auth_token="test-token",
            ),
        )
    )
    for session_id in (RUNNING, STOPPED):
        runner.store.write(session_id, SessionRecord.from_spec(SPEC))
        # As a started runner holds every stored session.
        await runner._load(session_id)
    running = runner.sessions[RUNNING]
    running.adapter = RecordingAdapter()
    running.process = cast(HarnessProcess, RunningProcess())
    runner.store.write(FAILED_SETUP, SessionRecord.from_spec(SPEC, setup_script="exit 1"))
    await (await runner._load(FAILED_SETUP)).emit(event_pb2.SetupFinished(exit_code=1), sources=[])
    try:
        yield runner
    finally:
        await runner.stop()


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
async def service(runner: Runner) -> AsyncIterator[ScriptedService]:
    """The scripted service; the runner dials it once the test has written its script."""
    service = ScriptedService()
    server = grpc.aio.server()
    runner_control_pb2_grpc.add_RunnerControlServicer_to_server(service, server)
    async with serving(server, "127.0.0.1:0", grace_s=0) as port:
        client = RunnerControlClient(runner, target=f"127.0.0.1:{port}", bearer="test-placeholder", timing=TIMING)
        tasks: list[asyncio.Task[None]] = []
        service.dial = lambda: tasks.append(asyncio.create_task(client.run()))
        try:
            yield service
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def deliver(session_id: str, command: command_pb2.Command) -> control_pb2.ServiceFrame:
    return control_pb2.ServiceFrame(deliver=control_pb2.DeliverCommand(session_id=session_id, command=command))


def submit(command_id: str, text: str = "test input") -> command_pb2.Command:
    return command_pb2.Command(command_id=command_id, submit_input=command_pb2.SubmitInput(text=text))


async def test_a_redelivered_command_is_admitted_once_and_answered_from_the_journal(
    runner: Runner, service: ScriptedService
) -> None:
    service.frames += [deliver(RUNNING, submit("test-input")), deliver(RUNNING, submit("test-input"))]
    service.dial()
    async with asyncio.timeout(10):
        first, second = await service.receipts.get(), await service.receipts.get()
    assert first == second
    assert first.admitted.event.command_admitted.command == submit("test-input")
    assert (await runner.sessions[RUNNING].journal.admitted_entry("test-input")) == first.admitted
    assert service.hellos[0].capabilities == ["commands", "lifecycle"]
    assert service.authorizations == ["Bearer test-placeholder"]


@pytest.mark.parametrize("ending", ["stopped", "sealed"])
async def test_a_redelivery_after_the_harness_ended_is_answered_with_the_original_receipt(
    ending: str, runner: Runner, service: ScriptedService
) -> None:
    # The service settles a submission whose receipt was lost only by delivering it again.
    service.frames.append(deliver(RUNNING, submit("test-input")))
    service.dial()
    async with asyncio.timeout(10):
        pushed = await service.connected.get()
        first = await service.receipts.get()
    session = runner.sessions[RUNNING]
    if ending == "stopped":
        assert session.process is not None
        await session.process.stop()
    else:
        pushed.put_nowait(seal("test-seal"))
        await control_results(service, 1)
    assert not session.running
    pushed.put_nowait(deliver(RUNNING, submit("test-input")))
    async with asyncio.timeout(10):
        assert await service.receipts.get() == first
    assert first.HasField("admitted")


async def test_refusals_name_their_cause(service: ScriptedService) -> None:
    service.frames += [
        deliver(RUNNING, submit("test-conflict", "first")),
        deliver(RUNNING, submit("test-conflict", "different")),
        deliver(STOPPED, submit("test-stopped")),
        deliver("test-unknown-session", submit("test-unknown")),
        deliver(RUNNING, command_pb2.Command(command_id="test-invalid")),
    ]
    service.dial()
    async with asyncio.timeout(10):
        receipts = [await service.receipts.get() for _ in range(5)]
    outcomes = {(r.command_id, r.refused.reason if r.HasField("refused") else None) for r in receipts}
    assert outcomes == {
        ("test-conflict", None),
        ("test-conflict", Reason.REASON_COMMAND_CONFLICT),
        ("test-stopped", Reason.REASON_HARNESS_STOPPED),
        ("test-unknown", Reason.REASON_SESSION_UNKNOWN),
        ("test-invalid", Reason.REASON_INVALID_COMMAND),
    }


@pytest.mark.parametrize("ending", ["superseded", "silent"])
async def test_a_lost_connection_is_replaced_by_a_fresh_one(ending: str, service: ScriptedService) -> None:
    if ending == "superseded":
        service.closes.append(grpc.StatusCode.ABORTED)
    else:
        service.silences = 1
    service.frames.append(deliver(RUNNING, submit("test-after-reconnect")))
    service.dial()
    async with asyncio.timeout(10):
        assert (await service.receipts.get()).HasField("admitted")
    assert len(service.hellos) == 2
    # One runner process: the same boot ID, over a new HTTP/2 connection.
    assert service.hellos[0].runner_boot_id == service.hellos[1].runner_boot_id
    assert service.peers[0] != service.peers[1]


def control(request: control_pb2.ControlRequest) -> control_pb2.ServiceFrame:
    return control_pb2.ServiceFrame(control=request)


def open_session(
    request_id: str, session_id: str, spec: runner_pb2.SessionSpec = SPEC, setup_script: str | None = None
) -> control_pb2.ServiceFrame:
    return control(
        control_pb2.ControlRequest(
            request_id=request_id,
            open=control_pb2.OpenSession(session_id=session_id, spec=spec, setup_script=setup_script),
        )
    )


def resume(request_id: str, session_id: str) -> control_pb2.ServiceFrame:
    return control(
        control_pb2.ControlRequest(request_id=request_id, resume=control_pb2.ResumeSession(session_id=session_id))
    )


def seal(request_id: str) -> control_pb2.ServiceFrame:
    return control(control_pb2.ControlRequest(request_id=request_id, seal=runner_pb2.SealRequest()))


async def control_results(service: ScriptedService, count: int) -> dict[str, control_pb2.ControlResult]:
    async with asyncio.timeout(10):
        results = [await service.control_results.get() for _ in range(count)]
    by_id = {result.request_id: result for result in results}
    assert len(by_id) == count
    return by_id


async def test_list_sessions_reports_every_session(service: ScriptedService) -> None:
    service.frames.append(
        control(control_pb2.ControlRequest(request_id="test-list", list_sessions=runner_pb2.ListSessionsRequest()))
    )
    service.dial()
    listed = (await control_results(service, 1))["test-list"]
    assert {(row.session_id, row.harness_state, row.setup_state) for row in listed.list_sessions.sessions} == {
        (RUNNING, runner_pb2.HARNESS_STATE_RUNNING, runner_pb2.SETUP_STATE_NOT_REQUIRED),
        (STOPPED, runner_pb2.HARNESS_STATE_STOPPED, runner_pb2.SETUP_STATE_NOT_REQUIRED),
        (FAILED_SETUP, runner_pb2.HARNESS_STATE_STOPPED, runner_pb2.SETUP_STATE_FAILED),
    }


async def test_open_creates_a_session_and_starts_it(
    runner: Runner, service: ScriptedService, started: list[str]
) -> None:
    service.frames.append(open_session("test-open", "test-new-session"))
    service.dial()
    opened = (await control_results(service, 1))["test-open"]
    assert (opened.open.session_id, opened.open.spec, opened.open.harness_state) == (
        "test-new-session",
        SPEC,
        runner_pb2.HARNESS_STATE_RUNNING,
    )
    assert started == ["test-new-session"]
    assert runner.store.exists("test-new-session")


async def test_opening_an_existing_session_confirms_it_without_restarting_its_harness(
    service: ScriptedService, started: list[str]
) -> None:
    service.frames.append(open_session("test-open", STOPPED))
    service.dial()
    opened = (await control_results(service, 1))["test-open"]
    assert (opened.open.session_id, opened.open.harness_state) == (STOPPED, runner_pb2.HARNESS_STATE_STOPPED)
    assert started == []


@pytest.mark.parametrize(
    ("spec", "setup_script"),
    [
        (runner_pb2.SessionSpec(harness=SPEC.harness, cwd=SPEC.cwd, model="test-other-model"), None),
        (SPEC, "test other setup"),
    ],
)
async def test_opening_an_existing_session_with_another_spec_is_refused(
    spec: runner_pb2.SessionSpec, setup_script: str | None, service: ScriptedService, started: list[str]
) -> None:
    service.frames.append(open_session("test-open", STOPPED, spec, setup_script))
    service.dial()
    assert (await control_results(service, 1))["test-open"].refused.reason == Refused.REASON_SPEC_CONFLICT
    assert started == []


async def test_resume_restarts_a_stopped_harness_on_its_retained_spec(
    service: ScriptedService, started: list[str]
) -> None:
    service.frames.append(resume("test-resume", STOPPED))
    service.dial()
    resumed = (await control_results(service, 1))["test-resume"]
    assert (resumed.resume.spec, resumed.resume.harness_state) == (SPEC, runner_pb2.HARNESS_STATE_RUNNING)
    assert started == [STOPPED]


@pytest.mark.parametrize(
    ("session_id", "reason"),
    [(FAILED_SETUP, Refused.REASON_SETUP_FAILED), ("test-unknown-session", Refused.REASON_SESSION_UNKNOWN)],
)
async def test_a_resume_the_runner_cannot_serve_is_refused(
    session_id: str, reason: control_pb2.ControlRefused.Reason, service: ScriptedService, started: list[str]
) -> None:
    service.frames.append(resume("test-resume", session_id))
    service.dial()
    assert (await control_results(service, 1))["test-resume"].refused.reason == reason
    assert started == []


async def test_a_seal_lands_after_the_commands_delivered_before_it_and_before_those_after_it(
    runner: Runner, service: ScriptedService, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Holds the earlier command's admission until the runner has read the Seal behind it.
    journal = runner.sessions[RUNNING].journal
    unblocked = asyncio.Event()
    get = journal.get

    async def held_get(command_id: str) -> object:
        await unblocked.wait()
        return await get(command_id)

    monkeypatch.setattr(journal, "get", held_get)
    # The runner handles each frame before reading the next, so asking for the frame after the
    # later delivery shows the Seal and that delivery were both read.
    read_through: list[control_pb2.ServiceFrame] = []
    read_past_last = asyncio.Event()
    serve = RunnerControlClient.serve

    class ObservedCall:
        def __init__(self, call: Any) -> None:
            self.call = call

        def __getattr__(self, name: str) -> Any:
            return getattr(self.call, name)

        async def read(self) -> object:
            if len(read_through) == 3:
                read_past_last.set()
            frame = await self.call.read()
            if isinstance(frame, control_pb2.ServiceFrame) and frame.WhichOneof("frame") != "heartbeat":
                read_through.append(frame)
            return frame

    async def observed_serve(client: RunnerControlClient, call: Any, welcome: control_pb2.Welcome) -> None:
        await serve(client, cast(Any, ObservedCall(call)), welcome)

    monkeypatch.setattr(RunnerControlClient, "serve", observed_serve)
    service.frames += [
        deliver(RUNNING, submit("test-before-seal")),
        seal("test-seal"),
        deliver(RUNNING, submit("test-after-seal")),
    ]
    service.dial()
    async with asyncio.timeout(10):
        await read_past_last.wait()
    unblocked.set()
    async with asyncio.timeout(10):
        receipts = {receipt.command_id: receipt for receipt in [await service.receipts.get() for _ in range(2)]}
    sealed = (await control_results(service, 1))["test-seal"]
    cursors = {row.session_id: row.cursor for row in sealed.seal.sessions}
    admitted = receipts["test-before-seal"]
    assert admitted.HasField("admitted")
    assert admitted.admitted.cursor <= cursors[RUNNING]
    assert receipts["test-after-seal"].refused.reason == Reason.REASON_HARNESS_STOPPED


async def test_sealing_again_returns_the_same_cursors_and_opening_is_refused(
    runner: Runner, service: ScriptedService, started: list[str]
) -> None:
    service.frames += [seal("test-seal-1"), seal("test-seal-2"), open_session("test-open", "test-new-session")]
    service.dial()
    results = await control_results(service, 3)
    assert results["test-seal-1"].seal == results["test-seal-2"].seal
    assert {row.session_id for row in results["test-seal-1"].seal.sessions} == {RUNNING, STOPPED, FAILED_SETUP}
    assert results["test-open"].refused.reason == Refused.REASON_SEALED
    assert not runner.sessions[RUNNING].running
    assert started == []


async def test_a_control_request_on_a_connection_that_did_not_agree_lifecycle_fails_the_stream(
    runner: Runner, service: ScriptedService
) -> None:
    service.capabilities = ["commands"]
    service.frames.append(seal("test-seal"))
    service.dial()
    async with asyncio.timeout(10):
        while len(service.hellos) < 2:
            await service.connected.get()
    assert service.control_results.empty()
    assert not runner.store.sealed


if __name__ == "__main__":
    pytest_bazel.main()
