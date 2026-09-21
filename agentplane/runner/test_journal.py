"""SQLite transaction visibility, failed commits, replay, and immutable command identity.

Commit gates exercise the real SQLite driver. They are not physical power-loss simulation:
durability beyond process death relies on SQLite EXTRA synchronization and the surviving volume.
"""

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import aiosqlite
import pytest
import pytest_bazel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from agentplane.protocol import command_pb2, event_log_pb2, event_pb2
from agentplane.runner.journal import Command, CommandConflictError, EventEntry, Journal, JournalStorageError
from agentplane.runner.observation import Observation

# gazelle:include_dep @pypi//protobuf


@dataclass
class CommitGate:
    reached: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    armed: bool = False
    fail: bool = False
    persisted: bool = False


@pytest.fixture
def commit_gate(monkeypatch: pytest.MonkeyPatch) -> CommitGate:
    gate = CommitGate()
    commit = aiosqlite.Connection.commit

    async def gated(connection: aiosqlite.Connection) -> None:
        if not gate.armed:
            await commit(connection)
            return
        gate.armed = False
        if gate.persisted:
            await commit(connection)
        gate.reached.set()
        await gate.release.wait()
        if gate.fail:
            raise sqlite3.OperationalError("injected commit failure")
        if not gate.persisted:
            await commit(connection)

    monkeypatch.setattr(aiosqlite.Connection, "commit", gated)
    return gate


@pytest.fixture
async def journal(tmp_path: Path) -> AsyncIterator[Journal]:
    async with Journal.open(tmp_path / "journal.sqlite", "test-source") as opened:
        yield opened


@pytest.mark.parametrize(
    "observation",
    [
        event_pb2.Native(direction=event_pb2.DIRECTION_FROM_HARNESS, line='{"text":"雪"}'),
        event_pb2.ItemStarted(item_id="test-item", kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT),
        event_pb2.TextDelta(item_id="test-item", text="hello"),
        event_pb2.ToolArgumentsDelta(item_id="test-tool", partial_json='{"command":'),
        event_pb2.ToolArguments(item_id="test-tool", arguments_json='{"command":"true"}'),
        event_pb2.ToolOutputDelta(item_id="test-tool", text="output"),
        event_pb2.ItemCompleted(item_id="test-item", text="hello"),
    ],
)
async def test_publication_waits_for_committed_replay(
    journal: Journal, commit_gate: CommitGate, tmp_path: Path, observation: Observation
) -> None:
    first = await journal.append(event_pb2.HarnessStarted(pid=123))
    follower = asyncio.create_task(journal.wait_beyond(first.cursor))
    commit_gate.armed = True
    append = asyncio.create_task(journal.append(observation))
    await commit_gate.reached.wait()
    assert (await journal.since(0, limit=128)) == [first]
    assert not follower.done()
    async with Journal.open(tmp_path / "journal.sqlite", "test-source") as reader:
        assert (await reader.since(0, limit=128)) == [first]
    commit_gate.release.set()
    published = await append
    await follower
    async with Journal.open(tmp_path / "journal.sqlite", "test-source") as reader:
        assert [entry.SerializeToString() for entry in (await reader.since(0, limit=128))] == [
            first.SerializeToString(),
            published.SerializeToString(),
        ]


@pytest.mark.parametrize("persisted", [False, True])
async def test_failed_commit_stops_publication_and_recovery_preserves_the_committed_prefix(
    tmp_path: Path, commit_gate: CommitGate, persisted: bool
) -> None:
    path = tmp_path / "journal.sqlite"
    async with Journal.open(path, "test-source") as journal:
        first = await journal.append(event_pb2.Native(line="committed"))
        follower = asyncio.create_task(journal.wait_beyond(first.cursor))
        commit_gate.armed, commit_gate.fail, commit_gate.persisted = True, True, persisted
        append = asyncio.create_task(journal.append(event_pb2.TextDelta(item_id="test-item", text="attempted")))
        await commit_gate.reached.wait()
        commit_gate.release.set()
        with pytest.raises(JournalStorageError):
            await append
        assert (await journal.since(0, limit=128)) == [first]
        with pytest.raises(JournalStorageError, match="reopen"):
            await follower
        with pytest.raises(JournalStorageError, match="reopen"):
            await journal.append(event_pb2.HarnessLost())
    async with Journal.open(path, "test-source") as recovered:
        assert (await recovered.since(0, limit=128))[0] == first
        assert recovered.last_cursor == 1 + int(persisted)
        next_cursor = recovered.last_cursor + 1
        assert (await recovered.append(event_pb2.HarnessLost())).cursor == next_cursor


async def test_coalesced_outcome_and_all_origins_commit_atomically(
    journal: Journal, commit_gate: CommitGate, tmp_path: Path
) -> None:
    commands = [
        command_pb2.Command(command_id=f"test-{i}", submit_input=command_pb2.SubmitInput(text=str(i))) for i in range(3)
    ]
    for command in commands:
        await journal.admit(command)
    outcome = event_pb2.HarnessUserMessageConfirmed(
        harness_message_id="test-message",
        text="0\n2",
        origin_command_ids=[commands[0].command_id, commands[2].command_id],
    )
    commit_gate.armed = True
    task = asyncio.create_task(journal.append(outcome, terminal_command_ids=outcome.origin_command_ids))
    await commit_gate.reached.wait()
    async with Journal.open(tmp_path / "journal.sqlite", "test-source") as reader:
        assert reader.last_cursor == 3
        assert await reader.pending_commands() == commands
    assert journal.last_cursor == 3
    commit_gate.release.set()
    receipt = await task
    assert await journal.pending_commands() == [commands[1]]
    for command in (commands[0], commands[2]):
        stored = await journal.get(command.command_id)
        assert stored is not None
        assert stored.terminal_cursor == receipt.cursor


async def test_admission_dedup_uses_the_full_frozen_command(journal: Journal, tmp_path: Path) -> None:
    command = command_pb2.Command(command_id="test-input", submit_input=command_pb2.SubmitInput(text="hello"))
    original = command.SerializeToString()
    admission = await journal.admit(command)
    assert admission is not None
    command.submit_input.text = "mutated by caller"
    assert admission.event.command_admitted.command.SerializeToString() == original
    assert await journal.admit(command_pb2.Command.FromString(original)) is None
    with pytest.raises(CommandConflictError):
        await journal.admit(command)
    # Mutating a public replay object cannot rewrite future replay or the canonical command.
    (await journal.since(0, limit=128))[0].event.command_admitted.command.submit_input.text = "mutated replay"
    async with Journal.open(tmp_path / "journal.sqlite", "test-source") as recovered:
        assert (await recovered.since(0, limit=128)) == [admission]
        assert await recovered.admit(command_pb2.Command.FromString(original)) is None


async def test_concurrent_admission_has_one_identity_and_contiguous_publication(journal: Journal) -> None:
    command = command_pb2.Command(command_id="test-input", submit_input=command_pb2.SubmitInput(text="hello"))
    admissions = await asyncio.gather(*(journal.admit(command) for _ in range(8)))
    assert sum(entry is not None for entry in admissions) == 1
    await asyncio.gather(*(journal.append(event_pb2.TextDelta(item_id="test-item", text=str(i))) for i in range(8)))
    assert [entry.cursor for entry in (await journal.since(0, limit=128))] == list(range(1, 10))
    assert (await journal.since(0, limit=128))[0].event.command_admitted.command == command


async def test_cancelled_commit_requires_recovery_even_if_sqlite_committed(
    tmp_path: Path, commit_gate: CommitGate
) -> None:
    path = tmp_path / "journal.sqlite"
    async with Journal.open(path, "test-source") as journal:
        commit_gate.armed, commit_gate.persisted = True, True
        command = command_pb2.Command(command_id="test-input", submit_input=command_pb2.SubmitInput(text="hello"))
        task = asyncio.create_task(journal.admit(command))
        await commit_gate.reached.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert journal.last_cursor == 0
        with pytest.raises(JournalStorageError):
            await journal.admit(command)
    async with Journal.open(path, "test-source") as recovered:
        assert recovered.last_cursor == 1
        assert await recovered.admit(command) is None
        assert (await recovered.since(0, limit=128))[0].event.command_admitted.command == command


async def test_invalid_multi_command_outcome_rolls_back_earlier_updates(journal: Journal) -> None:
    command = command_pb2.Command(command_id="test-input", submit_input=command_pb2.SubmitInput(text="hello"))
    await journal.admit(command)
    with pytest.raises(ValueError, match="unknown command"):
        await journal.append(
            event_pb2.HarnessUserMessageConfirmed(), terminal_command_ids=[command.command_id, "missing"]
        )
    assert journal.last_cursor == 1
    assert await journal.pending_commands() == [command]
    assert (await journal.append(event_pb2.HarnessLost())).cursor == 2


async def test_stale_storage_owner_cannot_publish_a_gap(journal: Journal, tmp_path: Path) -> None:
    async with Journal.open(tmp_path / "journal.sqlite", "test-source") as other:
        await journal.append(event_pb2.HarnessStarted(pid=123))
        with pytest.raises(JournalStorageError):
            await other.append(event_pb2.HarnessLost())
        assert (await other.since(0, limit=128)) == []
    assert (await journal.append(event_pb2.TextDelta(item_id="test-item", text="next"))).cursor == 2


async def test_sqlite_runtime_and_storage_contract(journal: Journal, tmp_path: Path) -> None:
    assert (await journal._connection.execute(text("PRAGMA synchronous"))).scalar_one() == 3
    await journal._connection.rollback()
    await journal.admit(
        command_pb2.Command(command_id="test-stop", stop_runner_session=command_pb2.StopRunnerSession())
    )
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'journal.sqlite'}")
    try:
        async with AsyncSession(engine) as reader:
            assert (await reader.execute(text("PRAGMA journal_mode"))).scalar_one() == "delete"
            row = (await reader.scalars(select(Command))).one()
            entry = await reader.get(EventEntry, row.admitted_cursor)
            assert entry is not None
            assert (
                event_log_pb2.EventEntry.FromString(entry.payload).event.command_admitted.command.SerializeToString()
                == row.payload
            )
    finally:
        await engine.dispose()


async def test_recovery_reads_checkpoint_and_replay_reads_only_requested_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "long.sqlite"
    async with Journal.open(path, "long-source") as journal:
        await journal.append(event_pb2.HarnessStarted(pid=123))
        await journal.append(event_pb2.TurnStarted(turn_id="active-turn"))
        for _ in range(260):
            await journal.append(event_pb2.Native(line="old history"))
        await journal.append(event_pb2.DebugCheckpoint(name="boundary", command_id="old-command"))
    decoded: list[int] = []
    decode = Journal._decode

    def counting_decode(payload: bytes, source_id: str, cursor: int) -> event_log_pb2.EventEntry:
        decoded.append(cursor)
        return decode(payload, source_id, cursor)

    monkeypatch.setattr(Journal, "_decode", staticmethod(counting_decode))
    async with Journal.open(path, "long-source") as recovered:
        assert decoded == [263]
        assert recovered.recovery_state.harness_running
        assert recovered.recovery_state.active_turn_id == "active-turn"
        assert await recovered.reached_checkpoint("boundary", "old-command")
        assert not await recovered.reached_checkpoint("boundary", "other-command")
        page = await recovered.since(128, limit=30)
        assert [entry.cursor for entry in page] == list(range(129, 159))
        assert decoded == [263, *range(129, 159)]
        assert await recovered.since(263, limit=30) == []
        await recovered.append(event_pb2.HarnessLost())
        assert not recovered.recovery_state.harness_running
        assert recovered.recovery_state.active_turn_id == "active-turn"
        await recovered.append(event_pb2.TurnCompleted(turn_id="active-turn"))
    async with Journal.open(path, "long-source") as recovered:
        assert recovered.recovery_state.active_turn_id == ""
        assert not recovered.recovery_state.harness_running
    with pytest.raises(ValueError, match="source"):
        async with Journal.open(path, "wrong-source"):
            pass


@pytest.mark.parametrize(("after", "limit"), [(-1, 1), (1, 1), (0, 0)])
async def test_replay_rejects_invalid_page(journal: Journal, after: int, limit: int) -> None:
    with pytest.raises(ValueError, match="page size"):
        await journal.since(after, limit=limit)


if __name__ == "__main__":
    pytest_bazel.main()
