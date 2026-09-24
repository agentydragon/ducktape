"""The stdout reader commits the lines it has read, with what the adapter derives from them, at once.

A scripted harness writes whatever the test asks for in a single write, so the lines arrive
together; the adapter derives one Event from each frame and answers a frame that asks.
"""

from __future__ import annotations

import json
import sys
import textwrap
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite
import pytest
import pytest_bazel
from pydantic import BaseModel

from agentplane.protocol import event_log_pb2, event_pb2
from agentplane.runner.adapter import HarnessAdapter
from agentplane.runner.config import RunnerConfig
from agentplane.runner.journal import Journal
from agentplane.runner.session import Session
from agentplane.runner.store import SessionRecord, SessionStore, StateOwner

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


class DerivingAdapter(HarnessAdapter):
    def __init__(self, session: Session) -> None:
        self.session = session

    def command(self) -> list[str]:
        return [sys.executable, "-c", HARNESS]

    def environment(self) -> Mapping[str, str]:
        return {}

    async def handshake(self) -> str:
        return "test-native-session"

    async def submit(self, command_id: str, text: str) -> None:
        raise AssertionError(f"unexpected input {(command_id, text)!r}")

    async def interrupt(self, turn_id: str) -> None:
        raise AssertionError(f"unexpected interrupt {turn_id!r}")

    async def change_model(self, command_id: str, model: str) -> None:
        raise AssertionError(f"unexpected model command {(command_id, model)!r}")

    async def on_frame(self, frame: dict[str, Any], source_sequence: int) -> None:
        await self.session.emit(event_pb2.TextDelta(item_id="test-item", text=str(frame["n"])))
        if frame.get("ask"):
            await self.session.send(Answer(answer=frame["n"]))


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
async def session(tmp_path: Path) -> AsyncIterator[Session]:
    state_dir = tmp_path / "state"
    store = SessionStore(state_dir / "sessions")
    owner = StateOwner(state_dir)
    record = SessionRecord(
        harness="HARNESS_CODEX", cwd=str(tmp_path / "workspace"), model="test-model", reasoning_effort="low"
    )
    store.write("test-session", record)
    try:
        async with Journal.open(
            store.directory("test-session") / "journal.sqlite", str(record.event_source_id)
        ) as journal:
            session = Session(
                "test-session",
                record=record,
                journal=journal,
                store=store,
                config=RunnerConfig(state_dir=state_dir),
                make_adapter=DerivingAdapter,
                state_owner_descriptor=owner.descriptor,
            )
            await session.ensure_running()
            try:
                yield session
            finally:
                await session.stop()
    finally:
        owner.close()


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


async def test_a_request_receives_its_reply_only_once_the_reply_is_committed(session: Session) -> None:
    receipt = await session.request(
        Write(write=_lines({"n": 0}, {"n": 1}, {"n": 2})), matches=lambda frame: frame.get("n") == 1
    )
    assert receipt.frame == {"n": 1}
    assert receipt.sequence <= session.journal.last_cursor


async def test_an_ordered_reply_is_handled_before_the_frames_after_it_are_translated(session: Session) -> None:
    before = session.journal.last_cursor
    request = Write(write=_lines({"n": 0}, {"n": 1}, {"n": 2}))
    async with session.ordered_reply():
        receipt = await session.request(request, matches=lambda frame: frame.get("n") == 1)
        await session.emit(event_pb2.TextDelta(item_id="test-item", text="handled"), sources=[receipt.sequence])
    await _published_through(session, before + 8)

    assert [_summary(entry) for entry in await session.journal.since(before, limit=128)] == [
        ("native DIRECTION_TO_HARNESS", request.model_dump_json()),
        ("native DIRECTION_FROM_HARNESS", json.dumps({"n": 0})),
        ("text_delta", "0"),
        ("native DIRECTION_FROM_HARNESS", json.dumps({"n": 1})),
        ("text_delta", "1"),
        ("text_delta", "handled"),
        ("native DIRECTION_FROM_HARNESS", json.dumps({"n": 2})),
        ("text_delta", "2"),
    ]


if __name__ == "__main__":
    pytest_bazel.main()
