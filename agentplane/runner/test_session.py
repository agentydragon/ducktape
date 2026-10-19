"""The stdout reader commits the lines it has read, with what the adapter derives from them, at once,
and a harness that dies under a command's input fails that command. A Session's first launch outcome
is read back from its journal.

A scripted harness writes whatever the test asks for in a single write, so the lines arrive
together; the adapter derives one Event from each frame and answers a frame that asks.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import textwrap
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite
import pytest
import pytest_bazel
from pydantic import BaseModel

from agentplane.proto import command_pb2, event_log_pb2, event_pb2, runner_pb2
from agentplane.runner.adapter import HarnessAdapter
from agentplane.runner.codex import CodexAdapter
from agentplane.runner.config import CodexLaunch, RunnerConfig
from agentplane.runner.journal import Journal
from agentplane.runner.observation import Observation
from agentplane.runner.session import Session
from agentplane.runner.store import SessionRecord, SessionStore, StateOwner
from agentplane.runner.testing import launches
from agentplane.runner.testing.adapters import UnexpectedAdapter
from agentplane.runner.testing.sessions import codex_record, open_session

# The generated protocol stubs' own stub chain, which the mypy aspect resolves for direct deps only.
# gazelle:include_dep @pypi//protobuf

HARNESS = textwrap.dedent(
    """
    import json, os, sys

    for line in sys.stdin:
        if "write" in (frame := json.loads(line)):
            os.write(1, frame["write"].encode())
    """
)


class Write(BaseModel):
    """Asks the harness to write these bytes to its stdout in one write."""

    write: str


class Answer(BaseModel):
    answer: int


class DerivingAdapter(UnexpectedAdapter):
    def __init__(self, session: Session) -> None:
        self.session = session

    def command(self) -> list[str]:
        return [sys.executable, "-c", HARNESS]

    async def on_frame(self, frame: dict[str, Any], source_sequence: int) -> None:
        await self.session.emit(
            event_pb2.TextDelta(item_id="test-item", text=str(frame["n"])), sources=[source_sequence]
        )
        if frame.get("ask"):
            await self.session.send(Answer(answer=frame["n"]))


class DeafAdapter(DerivingAdapter):
    """Its harness never reads stdin, so an input larger than the pipe and the writer's buffer
    together keeps the write waiting until the harness is gone."""

    def command(self) -> list[str]:
        return [sys.executable, "-c", "import signal; signal.pause()"]

    async def submit(self, command_id: str, text: str) -> None:
        await self.session.send(Write(write=text))


class NativeThreadAdapter(UnexpectedAdapter):
    """Resumes the recorded thread when its Session says to, and starts `fresh-thread` otherwise."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.resuming = session.resuming

    def command(self) -> list[str]:
        return [sys.executable, "-c", "import signal; signal.pause()"]

    async def handshake(self) -> str:
        return (self.session.record.native_session_id or "") if self.resuming else "fresh-thread"


class FailingReconcileAdapter(NativeThreadAdapter):
    def command(self) -> list[str]:
        return [sys.executable, "-c", "import sys; sys.stdin.read()"]

    async def reconcile(self, turn_id: str, *, resumed: bool) -> event_pb2.ConversationReconciled:
        del turn_id, resumed
        raise RuntimeError("injected reconciliation failure")


class RecoveringCodexAdapter(CodexAdapter):
    """Use real Codex recovery with a stand-in process and handshake."""

    def command(self) -> list[str]:
        return [sys.executable, "-c", "import sys; sys.stdin.read()"]

    async def handshake(self) -> str:
        return self._thread_id


@dataclass
class Commits:
    count: int = 0


@pytest.fixture
def commits(monkeypatch: pytest.MonkeyPatch) -> Commits:
    counted = Commits()
    commit = aiosqlite.Connection.commit

    async def counting(connection: aiosqlite.Connection) -> None:
        counted.count += 1
        await commit(connection)

    monkeypatch.setattr(aiosqlite.Connection, "commit", counting)
    return counted


@pytest.fixture
def make_adapter() -> Callable[[Session], HarnessAdapter]:
    return DerivingAdapter


@pytest.fixture
async def session(tmp_path: Path, make_adapter: Callable[[Session], HarnessAdapter]) -> AsyncIterator[Session]:
    async with open_session(
        "test-session",
        codex_record(tmp_path / "workspace"),
        config=RunnerConfig(state_dir=tmp_path / "state", launch=launches.SANDBOX_LAUNCH),
        make_adapter=make_adapter,
    ) as session:
        await session.ensure_running()
        try:
            yield session
        finally:
            await session.stop()


def _lines(*frames: Mapping[str, object]) -> str:
    return "".join(f"{json.dumps(frame)}\n" for frame in frames)


def _summary(entry: event_log_pb2.EventEntry) -> tuple[str, str]:
    event = entry.event
    match event.WhichOneof("observation"):
        case "native":
            return f"native {event_pb2.Direction.Name(event.native.direction)}", event.native.line
        case "text_delta":
            return "text_delta", event.text_delta.text
        case other:
            raise AssertionError(f"unexpected {other} Event")


async def _published_through(session: Session, cursor: int) -> None:
    while session.journal.last_cursor < cursor:
        await session.journal.wait_beyond(session.journal.last_cursor)


@pytest.mark.parametrize("count", [1, 3])
async def test_the_lines_one_write_delivers_commit_together_in_order(
    session: Session, commits: Commits, count: int
) -> None:
    """A burst commits once. A lone line on a stream that then stays silent commits without
    waiting for company: the test's wait below would never end if it did."""
    before, committed = session.journal.last_cursor, commits.count
    await session.send(Write(write=_lines(*({"n": n} for n in range(count)))))
    await session.journal.wait_beyond(before + 1)

    assert session.journal.last_cursor == before + 1 + 2 * count
    assert commits.count == committed + 2  # the request's own record, then the burst
    burst = await session.journal.since(before + 1, limit=128)
    assert [_summary(entry) for entry in burst] == [
        summary
        for n in range(count)
        for summary in (("native DIRECTION_FROM_HARNESS", json.dumps({"n": n})), ("text_delta", str(n)))
    ]
    for native, derived in zip(burst[::2], burst[1::2], strict=True):
        assert list(derived.event.source_sequences) == [native.cursor]


async def test_an_answer_mid_batch_commits_the_batch_so_far_before_it_is_written(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert session.process is not None
    write_line = session.process.write_line
    published_at_write: dict[str, int] = {}

    async def recording(line: str) -> None:
        published_at_write[line] = session.journal.last_cursor
        await write_line(line)

    monkeypatch.setattr(session.process, "write_line", recording)
    before = session.journal.last_cursor
    request = Write(write=_lines({"n": 0}, {"n": 1, "ask": True}, {"n": 2}))
    await session.send(request)
    await _published_through(session, before + 8)

    answer = Answer(answer=1).model_dump_json()
    entries = await session.journal.since(before, limit=128)
    assert [_summary(entry) for entry in entries] == [
        ("native DIRECTION_TO_HARNESS", request.model_dump_json()),
        ("native DIRECTION_FROM_HARNESS", json.dumps({"n": 0})),
        ("text_delta", "0"),
        ("native DIRECTION_FROM_HARNESS", json.dumps({"n": 1, "ask": True})),
        ("text_delta", "1"),
        ("native DIRECTION_TO_HARNESS", answer),
        ("native DIRECTION_FROM_HARNESS", json.dumps({"n": 2})),
        ("text_delta", "2"),
    ]
    # Everything through the answer's own record, and nothing after it, was committed when it was written.
    assert published_at_write[answer] == entries[5].cursor


async def test_a_request_receives_its_reply_once_the_reply_and_its_translation_are_committed(session: Session) -> None:
    receipt = await session.request(
        Write(write=_lines({"n": 0}, {"n": 1}, {"n": 2})), matches=lambda frame: frame.get("n") == 1
    )
    published = session.journal.last_cursor
    assert receipt.frame == {"n": 1}
    [translation] = await session.journal.since(receipt.sequence, limit=1)
    assert _summary(translation) == ("text_delta", "1")
    assert translation.cursor <= published


@pytest.mark.parametrize("make_adapter", [DeafAdapter])
async def test_a_harness_that_dies_under_an_input_fails_its_command(session: Session) -> None:
    assert session.process is not None
    before = session.journal.last_cursor
    await session.command(
        command_pb2.Command(command_id="input-1", submit_input=command_pb2.SubmitInput(text="x" * (1 << 20)))
    )
    dispatch = session._normal_dispatch_task
    assert dispatch is not None
    # The admission, then the input's own record, which `send` writes only once it found the harness running.
    await _published_through(session, before + 2)
    os.kill(session.process.native_pid, signal.SIGKILL)
    await dispatch
    await asyncio.gather(*session._tasks)

    stored = await session.journal.get("input-1")
    assert stored is not None
    assert stored.terminal_cursor is not None
    (terminal,) = await session.journal.since(stored.terminal_cursor - 1, limit=1)
    assert terminal.event.command_failed.command_id == "input-1"
    assert not session.harness_running


@pytest.mark.parametrize(
    ("setup_script", "observations", "outcome"),
    [
        ("", [], runner_pb2.LAUNCH_OUTCOME_PENDING),
        ("exit 1", [event_pb2.SetupFinished(exit_code=1)], runner_pb2.LAUNCH_OUTCOME_FAILED),
        ("", [event_pb2.HarnessLaunchFailed(reason="test launch failure")], runner_pb2.LAUNCH_OUTCOME_FAILED),
        # A harness that started once stays a successful launch after it exits.
        (
            "true",
            [
                event_pb2.SetupFinished(exit_code=0),
                event_pb2.HarnessStarted(pid=8),
                event_pb2.HarnessExited(exit_code=1),
            ],
            runner_pb2.LAUNCH_OUTCOME_STARTED,
        ),
        # Only the first outcome counts.
        ("", [event_pb2.HarnessExited(exit_code=1), event_pb2.HarnessStarted(pid=8)], runner_pb2.LAUNCH_OUTCOME_FAILED),
    ],
)
async def test_a_sessions_launch_outcome_survives_a_restart(
    tmp_path: Path, setup_script: str, observations: list[Observation], outcome: runner_pb2.LaunchOutcome
) -> None:
    state_dir = tmp_path / "state"
    store = SessionStore(state_dir / "sessions")
    owner = StateOwner(state_dir)
    record = SessionRecord.from_spec(
        runner_pb2.SessionSpec(harness=runner_pb2.HARNESS_CODEX, cwd=str(tmp_path), model="test-model"),
        setup_script=setup_script,
    )
    store.write("test-session", record)

    def load(journal: Journal) -> Session:
        return Session(
            "test-session",
            record=record,
            journal=journal,
            store=store,
            config=RunnerConfig(state_dir=state_dir, launch=launches.SANDBOX_LAUNCH),
            make_adapter=DerivingAdapter,
            state_owner_descriptor=owner.descriptor,
            activity_changed=lambda: None,
        )

    journal_path = store.directory("test-session") / "journal.sqlite"
    try:
        async with Journal.open(journal_path, str(record.event_source_id)) as journal:
            live = load(journal)
            for observation in observations:
                await live.emit(observation, sources=[])
            assert live.launch_outcome == outcome
        async with Journal.open(journal_path, str(record.event_source_id)) as journal:
            restarted = load(journal)
            await restarted.load_prefix_state()
            assert restarted.launch_outcome == outcome
    finally:
        owner.close()


@pytest.mark.parametrize(
    ("started_turn", "expected"),
    [(False, (False, "fresh-thread", False)), (True, (True, "recorded-thread", True))],
    ids=["no-turn", "after-a-turn"],
)
async def test_a_restart_resumes_the_recorded_thread_only_once_a_turn_started(
    tmp_path: Path, started_turn: bool, expected: tuple[bool, str | None, bool]
) -> None:
    record = codex_record(tmp_path / "workspace")
    record.native_session_id = "recorded-thread"
    adapters: list[NativeThreadAdapter] = []

    def make_adapter(session: Session) -> HarnessAdapter:
        adapters.append(NativeThreadAdapter(session))
        return adapters[-1]

    config = RunnerConfig(state_dir=tmp_path / "state", launch=launches.SANDBOX_LAUNCH)
    async with open_session("test-session", record, config=config, make_adapter=make_adapter) as session:
        if started_turn:
            await session.emit(event_pb2.TurnStarted(turn_id="turn-1"), sources=[])
        await session.ensure_running()
        try:
            (adapter,) = adapters
            (started,) = (
                entry.event.harness_started
                for entry in await session.journal.since(0, limit=100)
                if entry.event.WhichOneof("observation") == "harness_started"
            )
            stored = SessionStore(config.state_dir / "sessions").read("test-session")
            assert (adapter.resuming, stored.native_session_id, started.resumed) == expected
        finally:
            await session.stop()


async def test_failed_recovery_reconciliation_stops_the_unready_harness(tmp_path: Path) -> None:
    record = codex_record(tmp_path / "workspace")
    record.native_session_id = "recorded-thread"
    config = RunnerConfig(state_dir=tmp_path / "state", launch=launches.SANDBOX_LAUNCH)

    async with open_session("test-session", record, config=config, make_adapter=FailingReconcileAdapter) as session:
        await session.emit(event_pb2.TurnStarted(turn_id="recovery-turn"), sources=[])

        await session._activate()

        assert not session.running
        assert session.process is None
        assert session.process_id is None
        assert session._process_state == runner_pb2.PROCESS_STATE_UNSPECIFIED
        entries = await session.journal.since(0, limit=128)
        (failure,) = [
            entry.event.harness_launch_failed for entry in entries if entry.event.HasField("harness_launch_failed")
        ]
        assert failure.reason == (
            "conversation recovery failed for turn 'recovery-turn': RuntimeError('injected reconciliation failure')"
        )
        assert any(
            entry.event.HasField("harness_exited") and entry.event.harness_exited.stopped_by_runner for entry in entries
        )


async def test_codex_startup_recovers_output_from_a_tool_started_in_an_earlier_turn(tmp_path: Path) -> None:
    record = codex_record(tmp_path / "workspace")
    record.native_session_id = "recorded-thread"
    launch = CodexLaunch(binary=Path(sys.executable), base_url="http://unused.invalid/v1", api_key="unused")
    config = RunnerConfig(state_dir=tmp_path / "state", launch=launches.SANDBOX_LAUNCH, codex=launch)
    async with open_session(
        "test-session", record, config=config, make_adapter=lambda session: RecoveringCodexAdapter(session, launch)
    ) as session:
        await session.emit(event_pb2.TurnStarted(turn_id="old"), sources=[])
        await session.emit(
            event_pb2.ItemStarted(
                item_id="background", kind=event_pb2.ITEM_KIND_TOOL_CALL, tool_name="commandExecution"
            ),
            sources=[],
        )
        await session.emit(event_pb2.ToolOutputDelta(item_id="background", text="before\n"), sources=[])
        await session.turn_completed("old", event_pb2.TURN_STATUS_COMPLETED, "", sources=[])
        await session.emit(event_pb2.TurnStarted(turn_id="lost"), sources=[])
        await session.emit(event_pb2.ToolOutputDelta(item_id="background", text="during\n"), sources=[])
        await session.turn_completed("lost", event_pb2.TURN_STATUS_PROCESS_LOST, "", sources=[])
        history = session.directory / "codex" / "sessions" / "recorded-thread.jsonl"
        history.parent.mkdir(parents=True)
        history.write_text(
            "".join(
                json.dumps({"type": "response_item", "payload": payload}) + "\n"
                for payload in [
                    {"type": "function_call", "call_id": "background"},
                    {"type": "function_call_output", "call_id": "background", "output": "before\nduring\n"},
                ]
            )
        )

        before_resume = session.journal.last_cursor
        await session._activate()
        try:
            assert session.ready
            assert session.running
            assert await session.journal.unreconciled_process_lost_turns() == []
            entries = await session.journal.since(before_resume, limit=128)
            reports = [
                entry.event.conversation_reconciled
                for entry in entries
                if entry.event.HasField("conversation_reconciled")
            ]
            assert [report.turn_id for report in reports] == ["old", "lost"]
            assert reports[-1].items == [
                event_pb2.ItemRecovery(item_id="background", disposition=event_pb2.RECOVERY_DISPOSITION_RETAINED)
            ]
            assert not any(entry.event.HasField("harness_launch_failed") for entry in entries)
        finally:
            await session.stop()


if __name__ == "__main__":
    pytest_bazel.main()
