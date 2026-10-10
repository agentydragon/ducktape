"""The runner's channel peer against a scripted Sandbox Service: delivered commands are admitted once
and answered from the journal, refusals name their cause, and a lost connection is replaced by a
fresh one."""

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast, override

import grpc
import pytest
import pytest_bazel

from agentplane.protocol import command_pb2, event_pb2
from agentplane.runner import protocol_pb2
from agentplane.runner.adapter import HarnessAdapter
from agentplane.runner.channel import ChannelTiming, RunnerChannelClient
from agentplane.runner.config import RunnerConfig
from agentplane.runner.harness_process import HarnessProcess
from agentplane.runner.service import Runner
from agentplane.runner.store import SessionRecord
from agentplane.runner_channel import protocol_pb2 as channel_pb2, protocol_pb2_grpc

# gazelle:include_dep @pypi//protobuf

RUNNING = "test-running-session"
STOPPED = "test-stopped-session"
SPEC = protocol_pb2.SessionSpec(harness=protocol_pb2.HARNESS_CLAUDE, cwd="/test-workspace", model="test-model")
TIMING = ChannelTiming(heartbeat_s=0.1, liveness_s=0.5, backoff_initial_s=0.01, backoff_max_s=0.05)
Reason = channel_pb2.CommandRefused


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
    running = True


@dataclass
class ScriptedService(protocol_pb2_grpc.RunnerChannelServicer):
    """Each connection gets Welcome, then its scripted frames, or the scripted status instead."""

    frames: list[channel_pb2.ServiceFrame] = field(default_factory=list)
    # Ends the next connections right after Welcome, oldest first.
    closes: list[grpc.StatusCode] = field(default_factory=list)
    # Sends nothing after Welcome on the next connections, oldest first.
    silences: int = 0
    hellos: list[channel_pb2.Hello] = field(default_factory=list)
    peers: list[str] = field(default_factory=list)
    authorizations: list[str] = field(default_factory=list)
    receipts: asyncio.Queue[channel_pb2.CommandReceipt] = field(default_factory=asyncio.Queue)
    dial: Callable[[], None] = lambda: None

    @override
    async def Connect(  # type: ignore[override]
        self, request_iterator: AsyncIterator[channel_pb2.RunnerFrame], context: grpc.aio.ServicerContext
    ) -> None:
        self.authorizations += [
            str(value) for key, value in context.invocation_metadata() or () if key == "authorization"
        ]
        self.hellos.append((await anext(request_iterator)).hello)
        self.peers.append(context.peer())
        await context.write(
            channel_pb2.ServiceFrame(
                welcome=channel_pb2.Welcome(connection_epoch=len(self.hellos), capabilities=["commands"])
            )
        )
        if self.closes:
            await context.abort(self.closes.pop(0), "test service closes this connection")
        if self.silences:
            self.silences -= 1
            await asyncio.Event().wait()
        for scripted in self.frames:
            await context.write(scripted)
        async for frame in request_iterator:
            if frame.HasField("receipt"):
                await self.receipts.put(frame.receipt)
            await context.write(channel_pb2.ServiceFrame(heartbeat=channel_pb2.Heartbeat()))


@pytest.fixture
async def runner(tmp_path: Path) -> AsyncIterator[Runner]:
    runner = Runner(RunnerConfig(state_dir=tmp_path / "state", harness_environment={}))
    for session_id in (RUNNING, STOPPED):
        runner.store.write(session_id, SessionRecord.from_spec(SPEC))
    running = await runner._load(RUNNING)
    running.adapter = RecordingAdapter()
    running.process = cast(HarnessProcess, RunningProcess())
    try:
        yield runner
    finally:
        running.process = None
        await runner.stop()


@pytest.fixture
async def service(runner: Runner) -> AsyncIterator[ScriptedService]:
    """The scripted service; the runner dials it once the test has written its script."""
    service = ScriptedService()
    server = grpc.aio.server()
    protocol_pb2_grpc.add_RunnerChannelServicer_to_server(service, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    client = RunnerChannelClient(runner, target=f"127.0.0.1:{port}", bearer="test-placeholder", timing=TIMING)
    tasks: list[asyncio.Task[None]] = []
    service.dial = lambda: tasks.append(asyncio.create_task(client.run()))
    try:
        yield service
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await server.stop(0)


def deliver(session_id: str, command: command_pb2.Command) -> channel_pb2.ServiceFrame:
    return channel_pb2.ServiceFrame(deliver=channel_pb2.DeliverCommand(session_id=session_id, command=command))


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
    assert service.hellos[0].capabilities == ["commands"]
    assert service.authorizations == ["Bearer test-placeholder"]


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


if __name__ == "__main__":
    pytest_bazel.main()
