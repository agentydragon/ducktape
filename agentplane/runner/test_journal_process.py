"""A killed journal writer and a fresh process agree on published Events and command outcomes, and
nothing of a batch the writer had not committed survives.

This is a process-restart check, not a power-loss test: killing a process leaves the kernel
write cache intact. SQLite EXTRA synchronization supplies the storage durability fence.
"""

import asyncio
import multiprocessing
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from multiprocessing.connection import Connection
from pathlib import Path

import pytest_bazel

from agentplane.protocol import command_pb2, event_pb2
from agentplane.runner.journal import Journal

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
    asyncio.run(_publish_async(root, connection))


async def _publish_async(root: Path, connection: Connection) -> None:
    async with Journal.open(root / "journal.sqlite", "test-source") as journal:
        command = command_pb2.Command(command_id="test-input", submit_input=command_pb2.SubmitInput(text="hello"))
        await journal.admit(command)
        await journal.dispatch_planned(command.command_id, native_correlation={"request_id": "test-native-request"})
        await journal.append(
            event_pb2.HarnessUserMessageConfirmed(
                harness_message_id="test-message", text="hello", origin_command_ids=[command.command_id]
            ),
            terminal_command_ids=[command.command_id],
        )
        await journal.append(event_pb2.Native(direction=event_pb2.DIRECTION_FROM_HARNESS, line='{"text":"output"}'))
        await journal.append(event_pb2.TextDelta(item_id="test-item", text="output"))
        published = b"\\n".join(entry.SerializeToString() for entry in (await journal.since(0, limit=128)))
        # The kill lands inside a batch that has written but not committed.
        async with journal.batch():
            await journal.append(event_pb2.Native(direction=event_pb2.DIRECTION_FROM_HARNESS, line='{"text":"more"}'))
            await journal.append(event_pb2.TextDelta(item_id="test-item", text="more"))
            connection.send_bytes(published)
            await asyncio.to_thread(connection.recv_bytes)  # Parent kills this process without cleanup.


def _recover(root: Path, connection: Connection) -> None:
    asyncio.run(_recover_async(root, connection))


async def _recover_async(root: Path, connection: Connection) -> None:
    async with Journal.open(root / "journal.sqlite", "test-source") as journal:
        stored = await journal.get("test-input")
        assert stored is not None
        assert stored.terminal_cursor == 2
        assert stored.dispatch_planned
        assert stored.native_correlation == {"request_id": "test-native-request"}
        assert await journal.admit(command_pb2.Command.FromString(stored.payload)) is None
        assert await journal.pending_commands() == []
        assert (await journal.since(0, limit=128))[
            1
        ].event.harness_user_message_confirmed == event_pb2.HarnessUserMessageConfirmed(
            harness_message_id="test-message", text="hello", origin_command_ids=[stored.command_id]
        )
        replay = b"\\n".join(entry.SerializeToString() for entry in (await journal.since(0, limit=128)))
        next_entry = await journal.append(event_pb2.ItemCompleted(item_id="test-item", text="output"))
        assert next_entry.cursor == 5
        connection.send_bytes(replay)


def test_new_process_replays_the_killed_writers_exact_published_prefix(tmp_path: Path) -> None:
    with journal_process(_publish, tmp_path) as producer:
        published = producer.recv_bytes()
    with journal_process(_recover, tmp_path) as successor:
        assert successor.recv_bytes() == published


if __name__ == "__main__":
    pytest_bazel.main()
