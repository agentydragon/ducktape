"""Published Events survive loss of all writes outside completed persistence fences."""

import asyncio
import os
from pathlib import Path

import pytest
import pytest_bazel

from x.agentplane.protocol import command_pb2, event_pb2
from x.agentplane.runner import protocol_pb2
from x.agentplane.runner.command_journal import CommandConflictError, CommandJournal
from x.agentplane.runner.event_log import EventLog, Observation
from x.agentplane.runner.store import SessionRecord, SessionStore
from x.agentplane.runner.testing.storage_image import StorageImage

# gazelle:include_dep @pypi//protobuf


@pytest.fixture
def storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StorageImage:
    root = tmp_path / "volume"
    root.mkdir()
    image = StorageImage(root)
    monkeypatch.setattr(os, "fsync", image.fsync)
    return image


@pytest.mark.parametrize(
    "observation",
    [
        event_pb2.Native(direction=event_pb2.DIRECTION_FROM_HARNESS, line='{"text":"hello"}'),
        event_pb2.ItemStarted(item_id="test-item", kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT),
        event_pb2.TextDelta(item_id="test-item", text="hello"),
        event_pb2.ToolArgumentsDelta(item_id="test-tool", partial_json='{"command":'),
        event_pb2.ToolArguments(item_id="test-tool", arguments_json='{"command":"true"}'),
        event_pb2.ToolOutputDelta(item_id="test-tool", text="output"),
        event_pb2.ItemCompleted(item_id="test-item", text="hello"),
    ],
)
async def test_streamed_entries_publish_after_their_recoverable_prefix(
    storage: StorageImage, tmp_path: Path, observation: Observation
) -> None:
    log = EventLog(storage.root / "events.jsonl", "test-source")
    first = log.append(event_pb2.HarnessStarted(pid=123))
    waiting = asyncio.Event()

    async def follow() -> None:
        waiting.set()
        await log.wait_beyond(first.cursor)

    follower = asyncio.create_task(follow())
    await waiting.wait()

    def before_commit(_path: Path) -> None:
        assert log.last_cursor == first.cursor
        assert log.since(0) == [first]
        assert not follower.done()

    storage.before_sync = before_commit
    published = log.append(observation)
    storage.before_sync = None
    await follower

    # No clean close or later lifecycle Event gets to flush this streaming suffix first.
    recovered_root = tmp_path / "recovered"
    storage.recover(recovered_root)
    recovered = EventLog(recovered_root / "events.jsonl", "test-source")
    assert recovered.entries == log.entries
    assert recovered.since(first.cursor) == [published]
    next_entry = recovered.append(event_pb2.HarnessLost())
    assert next_entry.cursor == published.cursor + 1
    recovered.close()
    log.close()


async def test_failed_fence_does_not_publish_or_allow_cursor_reuse(storage: StorageImage, tmp_path: Path) -> None:
    path = storage.root / "events.jsonl"
    log = EventLog(path, "test-source")
    committed = log.append(event_pb2.Native(direction=event_pb2.DIRECTION_FROM_HARNESS, line="committed"))
    waiting = asyncio.Event()

    async def follow() -> None:
        waiting.set()
        await log.wait_beyond(committed.cursor)

    follower = asyncio.create_task(follow())
    await waiting.wait()
    storage.fail_path = path

    with pytest.raises(OSError, match="injected storage fence failure"):
        log.append(event_pb2.TextDelta(item_id="test-item", text="uncommitted"))
    assert log.since(0) == [committed]
    with pytest.raises(OSError, match="reopen it for recovery"):
        await follower

    storage.fail_path = None
    with pytest.raises(OSError, match="reopen it for recovery"):
        log.append(event_pb2.TextDelta(item_id="test-item", text="cannot reuse cursor"))
    recovered_root = tmp_path / "recovered"
    storage.recover(recovered_root)
    recovered = EventLog(recovered_root / "events.jsonl", "test-source")
    assert recovered.entries == [committed]
    assert recovered.append(event_pb2.HarnessLost()).cursor == committed.cursor + 1
    recovered.close()
    log.close()


def test_failed_command_fence_retains_previous_identity_and_outcome(storage: StorageImage, tmp_path: Path) -> None:
    path = storage.root / "commands.jsonl"
    journal = CommandJournal(path)
    command = command_pb2.Command(command_id="test-input", submit_input=command_pb2.SubmitInput(text="hello"))
    journal.admit(command)
    journal.dispatch_planned(command.command_id, native_correlation={"request_id": "test-request"})
    outcome = event_pb2.HarnessUserMessageConfirmed(
        harness_message_id="test-message", text="hello", origin_command_ids=[command.command_id]
    )
    journal.terminal(command.command_id, outcome=outcome)
    committed = list(journal.entries)
    storage.fail_path = path
    later = command_pb2.Command(command_id="test-later", submit_input=command_pb2.SubmitInput(text="later"))
    with pytest.raises(OSError, match="injected storage fence failure"):
        journal.admit(later)
    assert journal.entries == committed
    storage.fail_path = None
    with pytest.raises(OSError, match="reopen it for recovery"):
        journal.admit(later)

    recovered_root = tmp_path / "recovered"
    storage.recover(recovered_root)
    recovered = CommandJournal(recovered_root / "commands.jsonl")
    assert recovered.entries == committed
    assert not recovered.admit(command)
    assert recovered.admit(later)
    with pytest.raises(CommandConflictError):
        recovered.admit(
            command_pb2.Command(command_id=command.command_id, change_model=command_pb2.ChangeModel(model="x"))
        )
    recovered.close()
    journal.close()


def test_new_session_and_replaced_metadata_remain_findable(storage: StorageImage, tmp_path: Path) -> None:
    store = SessionStore(storage.root / "state" / "sessions")
    record = SessionRecord.from_spec(protocol_pb2.SessionSpec(harness=protocol_pb2.HARNESS_CLAUDE, model="test-model"))
    store.write("test-session", record)
    record.native_session_id = "test-native-session"
    store.write("test-session", record)
    log = EventLog(store.directory("test-session") / "events.jsonl", str(record.event_source_id))
    published = log.append(event_pb2.Native(direction=event_pb2.DIRECTION_FROM_HARNESS, line="first"))

    recovered_root = tmp_path / "recovered"
    storage.recover(recovered_root)
    recovered_store = SessionStore(recovered_root / "state" / "sessions")
    assert recovered_store.session_ids() == ["test-session"]
    assert recovered_store.read("test-session") == record
    recovered = EventLog(recovered_store.directory("test-session") / "events.jsonl", str(record.event_source_id))
    assert recovered.entries == [published]
    recovered.close()
    log.close()


if __name__ == "__main__":
    pytest_bazel.main()
