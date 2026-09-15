"""A killed journal writer and a fresh process agree on published Events and command outcomes.

This is a process-restart check. test_event_durability separately discards unsynced filesystem
state; killing a process alone leaves the kernel write cache intact.
"""

import multiprocessing
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from multiprocessing.connection import Connection
from pathlib import Path

import pytest_bazel

from x.agentplane.protocol import command_pb2, event_pb2
from x.agentplane.runner.command_journal import CommandJournal
from x.agentplane.runner.event_log import EventLog

# gazelle:include_dep @pypi//protobuf


@contextmanager
def journal_process(target: Callable[[Path, Connection], None], root: Path) -> Iterator[Connection]:
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=target, args=(root, child))
    process.start()
    child.close()
    try:
        yield parent
    finally:
        if process.is_alive():
            process.kill()
        process.join()
        parent.close()
        process.close()


def _publish(root: Path, connection: Connection) -> None:
    journal = CommandJournal(root / "commands.jsonl")
    command = command_pb2.Command(command_id="test-input", submit_input=command_pb2.SubmitInput(text="hello"))
    journal.admit(command)
    outcome = event_pb2.HarnessUserMessageConfirmed(
        harness_message_id="test-message", text="hello", origin_command_ids=[command.command_id]
    )
    journal.terminal(command.command_id, outcome=outcome)
    log = EventLog(root / "events.jsonl", "test-source")
    log.append(event_pb2.CommandAdmitted(command=command))
    log.append(outcome)
    log.append(event_pb2.Native(direction=event_pb2.DIRECTION_FROM_HARNESS, line='{"text":"output"}'))
    log.append(event_pb2.TextDelta(item_id="test-item", text="output"))
    connection.send_bytes(b"\n".join(entry.SerializeToString() for entry in log.entries))
    connection.recv_bytes()  # The parent kills this writer without a close or another Event.


def _recover(root: Path, connection: Connection) -> None:
    journal = CommandJournal(root / "commands.jsonl")
    assert len(journal.entries) == 1
    admitted = journal.entries[0]
    assert admitted.state == "terminal"
    assert not journal.admit(admitted.command)
    assert admitted.outcome == event_pb2.HarnessUserMessageConfirmed(
        harness_message_id="test-message", text="hello", origin_command_ids=[admitted.command.command_id]
    )
    log = EventLog(root / "events.jsonl", "test-source")
    replay = b"\n".join(entry.SerializeToString() for entry in log.entries)
    next_entry = log.append(event_pb2.ItemCompleted(item_id="test-item", text="output"))
    assert next_entry.cursor == 5
    connection.send_bytes(replay)
    log.close()
    journal.close()


def test_new_process_replays_the_killed_writers_exact_published_prefix(tmp_path: Path) -> None:
    with journal_process(_publish, tmp_path) as producer:
        published = producer.recv_bytes()
    with journal_process(_recover, tmp_path) as successor:
        assert successor.recv_bytes() == published


if __name__ == "__main__":
    pytest_bazel.main()
