"""The runner's push of each Session's journal to a test ingress: it resumes after what the ingress
acknowledged, replays after a lost call, backs a refused Session off without holding up the others,
and reports `flushed` from the acknowledgements it recorded, across a restart."""

import asyncio
import dataclasses
import socket
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import override

import grpc
import pytest
import pytest_bazel

from agentplane.grpc_process import serving
from agentplane.proto import event_log_pb2, event_pb2, runner_ingress_pb2, runner_ingress_pb2_grpc, runner_pb2
from agentplane.runner.config import IngressEndpoint, RunnerConfig
from agentplane.runner.control import ControlTiming
from agentplane.runner.ingress import IngressPusher
from agentplane.runner.service import Runner
from agentplane.runner.store import SessionRecord

# gazelle:include_dep @pypi//protobuf

SPEC = runner_pb2.SessionSpec(harness=runner_pb2.HARNESS_CLAUDE, cwd="/test-workspace", model="test-model")
A = "test-session-a"
B = "test-session-b"
TIMING = ControlTiming(heartbeat_s=0.1, liveness_s=0.5, backoff_initial_s=0.01, backoff_max_s=0.05)
JOURNALED = 3


@dataclass
class ScriptedIngress(runner_ingress_pb2_grpc.RunnerIngressServicer):
    """Commits each entry as it arrives and acknowledges it, and repeats its latest acknowledgement
    on an idle call."""

    committed: dict[str, list[event_log_pb2.EventEntry]] = field(default_factory=dict)
    sealed: set[str] = field(default_factory=set)
    # Sessions whose calls end with PERMISSION_DENIED, as another writer's would.
    refused: set[str] = field(default_factory=set)
    # A first acknowledgement to send in place of what is committed.
    first_cursor: dict[str, int] = field(default_factory=dict)
    # Ends the Session's call, once, when this cursor arrives, without committing it.
    lose_at: dict[str, int] = field(default_factory=dict)
    # Every call's Session and the entry cursors it received, in order.
    calls: list[tuple[str, list[int]]] = field(default_factory=list)
    changed: asyncio.Condition = field(default_factory=asyncio.Condition)

    async def until(self, predicate: Callable[[], bool]) -> None:
        async with asyncio.timeout(10), self.changed:
            await self.changed.wait_for(predicate)

    def received(self, session_id: str) -> list[list[int]]:
        return [cursors for called, cursors in self.calls if called == session_id]

    async def _notify(self) -> None:
        async with self.changed:
            self.changed.notify_all()

    @override
    async def Push(  # type: ignore[override]
        self, request_iterator: AsyncIterator[runner_ingress_pb2.PushFrame], context: grpc.aio.ServicerContext
    ) -> None:
        session_id = (await anext(request_iterator)).announce.session_id
        received: list[int] = []
        self.calls.append((session_id, received))
        await self._notify()
        if session_id in self.refused:
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, "test ingress: another writer's Session")
        committed = self.committed.setdefault(session_id, [])
        writing = asyncio.Lock()

        async def acknowledge(cursor: int) -> None:
            async with writing:
                await context.write(runner_ingress_pb2.Acknowledgement(cursor=cursor, sealed=session_id in self.sealed))

        async def repeat() -> None:
            while True:
                await asyncio.sleep(TIMING.heartbeat_s)
                await acknowledge(len(committed))

        await acknowledge(self.first_cursor.get(session_id, len(committed)))
        repeating = asyncio.create_task(repeat())
        try:
            async for frame in request_iterator:
                match frame.WhichOneof("frame"):
                    case "entry":
                        received.append(frame.entry.cursor)
                        if self.lose_at.get(session_id) == frame.entry.cursor:
                            del self.lose_at[session_id]
                            await context.abort(grpc.StatusCode.UNAVAILABLE, "test ingress loses this call")
                        if frame.entry.cursor <= len(committed):
                            assert committed[frame.entry.cursor - 1] == frame.entry
                        else:
                            assert frame.entry.cursor == len(committed) + 1
                            committed.append(frame.entry)
                    case "sealed":
                        assert frame.sealed.cursor == len(committed)
                        self.sealed.add(session_id)
                    case "heartbeat":
                        continue
                await acknowledge(len(committed))
                await self._notify()
        finally:
            repeating.cancel()


@pytest.fixture
def config(tmp_path: Path) -> RunnerConfig:
    # Each test starts its pusher against its own ingress; the endpoint here only makes Seal say so.
    return RunnerConfig(
        state_dir=tmp_path / "state", ingress=IngressEndpoint(target="test-ingress.invalid:1", bearer="test-bearer")
    )


async def journal_stderr(runner: Runner, session_id: str, count: int) -> None:
    session = await runner.stored_session(session_id)
    assert session is not None
    for index in range(count):
        await session.journal.append(event_pb2.HarnessStderr(text=f"test line {index}"))


async def journal_of(runner: Runner, session_id: str) -> list[event_log_pb2.EventEntry]:
    journal = runner.sessions[session_id].journal
    return await journal.since(0, limit=journal.last_cursor or 1)


@pytest.fixture
async def runner(config: RunnerConfig) -> AsyncIterator[Runner]:
    runner = Runner(config)
    try:
        for session_id in (A, B):
            runner.store.write(session_id, SessionRecord.from_spec(SPEC))
            await journal_stderr(runner, session_id, JOURNALED)
        yield runner
    finally:
        await runner.stop()


@pytest.fixture
async def ingress() -> AsyncIterator[tuple[ScriptedIngress, str]]:
    service = ScriptedIngress()
    server = grpc.aio.server()
    runner_ingress_pb2_grpc.add_RunnerIngressServicer_to_server(service, server)
    async with serving(server, "127.0.0.1:0", grace_s=0) as port:
        yield service, f"127.0.0.1:{port}"


@asynccontextmanager
async def pushing(runner: Runner, target: str, timing: ControlTiming = TIMING) -> AsyncIterator[IngressPusher]:
    pusher = IngressPusher(runner, target=target, bearer="test-placeholder", timing=timing)
    task = asyncio.create_task(pusher.run())
    try:
        yield pusher
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_a_push_resumes_after_the_acknowledged_cursor_then_streams_live_entries(
    runner: Runner, ingress: tuple[ScriptedIngress, str]
) -> None:
    service, target = ingress
    await runner.stored_session(A)
    service.committed[A] = (await journal_of(runner, A))[:2]
    async with pushing(runner, target):
        await service.until(lambda: len(service.committed[A]) == JOURNALED)
        await journal_stderr(runner, A, 1)
        await service.until(lambda: len(service.committed[A]) == JOURNALED + 1)
    assert service.committed[A] == await journal_of(runner, A)
    assert service.received(A) == [[3, 4]]


async def test_a_lost_call_is_replayed_from_the_ingress_acknowledgement(
    runner: Runner, ingress: tuple[ScriptedIngress, str]
) -> None:
    service, target = ingress
    service.lose_at[A] = 2
    async with pushing(runner, target):
        await service.until(lambda: len(service.committed.get(A, [])) == JOURNALED)
    assert service.committed[A] == await journal_of(runner, A)
    assert service.received(A) == [[1, 2], [2, 3]]


async def test_a_session_created_while_pushing_is_pushed(runner: Runner, ingress: tuple[ScriptedIngress, str]) -> None:
    service, target = ingress
    async with pushing(runner, target):
        await service.until(lambda: len(service.committed.get(B, [])) == JOURNALED)
        runner.store.write("test-session-new", SessionRecord.from_spec(SPEC))
        await journal_stderr(runner, "test-session-new", 1)
        await service.until(lambda: len(service.committed.get("test-session-new", [])) == 1)


@pytest.mark.parametrize("refusal", ["another writer", "acknowledged beyond the journal"])
async def test_a_refused_session_backs_off_without_holding_up_the_others(
    refusal: str, runner: Runner, ingress: tuple[ScriptedIngress, str]
) -> None:
    service, target = ingress
    if refusal == "another writer":
        service.refused.add(A)
    else:
        service.first_cursor[A] = JOURNALED + 1
    # Only an operator resolves either refusal, so the next attempt waits the maximum, far past this test.
    async with pushing(runner, target, dataclasses.replace(TIMING, backoff_max_s=30)):
        await service.until(lambda: len(service.committed.get(B, [])) == JOURNALED)
        await journal_stderr(runner, B, 1)
        await service.until(lambda: len(service.committed[B]) == JOURNALED + 1)
        assert service.received(A) == [[]]
    assert not service.committed.get(A)


async def test_flushed_reports_the_acknowledged_seal_across_a_restart(
    config: RunnerConfig, runner: Runner, ingress: tuple[ScriptedIngress, str]
) -> None:
    service, target = ingress
    unflushed = await runner.seal()
    assert unflushed.pushes
    assert {seal.session_id: seal.flushed for seal in unflushed.sessions} == {A: False, B: False}
    async with pushing(runner, target) as pusher:
        await service.until(lambda: service.sealed == {A, B})
        async with asyncio.timeout(10):
            await asyncio.gather(*pusher.pushes.values())
    assert {seal.session_id: seal.flushed for seal in (await runner.seal()).sessions} == {A: True, B: True}
    await runner.stop()
    restarted = Runner(config)
    try:
        assert {seal.session_id: (seal.cursor, seal.flushed) for seal in (await restarted.seal()).sessions} == {
            A: (JOURNALED, True),
            B: (JOURNALED, True),
        }
    finally:
        await restarted.stop()


async def test_an_unreachable_ingress_holds_up_neither_journaling_nor_seal(runner: Runner) -> None:
    with socket.socket() as unused:
        unused.bind(("127.0.0.1", 0))
        target = f"127.0.0.1:{unused.getsockname()[1]}"
    async with pushing(runner, target):
        await journal_stderr(runner, A, 1)
        assert {seal.session_id: (seal.cursor, seal.flushed) for seal in (await runner.seal()).sessions} == {
            A: (JOURNALED + 1, False),
            B: (JOURNALED, False),
        }


if __name__ == "__main__":
    pytest_bazel.main()
