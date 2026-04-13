"""Tests for background task output collection via asyncio queues."""

import asyncio
from pathlib import Path

import pytest
import pytest_bazel

from devinfra.claude.hook_daemon.session import BgStream, Session, _feed_queue
from devinfra.claude.hook_daemon.testing.testing_helpers import TEST_PROFILE
from devinfra.claude.session_paths import SessionPaths


async def test_feed_queue_partial():
    """Lines available before EOF are collected without waiting for the process to finish."""
    reader = asyncio.StreamReader()
    reader.feed_data(b"line1\nline2\n")
    # No EOF — simulates an in-progress process

    queue: asyncio.Queue[str] = asyncio.Queue()
    feed_task = asyncio.create_task(_feed_queue(reader, queue))
    await asyncio.sleep(0)  # yield to let feed task run

    lines = []
    while not queue.empty():
        lines.append(queue.get_nowait())

    assert lines == ["line1", "line2"]
    assert not feed_task.done()  # still waiting for more input / EOF

    feed_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await feed_task


async def test_feed_queue_eof():
    reader = asyncio.StreamReader()
    reader.feed_data(b"hello\n")
    reader.feed_eof()

    queue: asyncio.Queue[str] = asyncio.Queue()
    feed_task = asyncio.create_task(_feed_queue(reader, queue))
    await feed_task  # exits at EOF

    assert queue.get_nowait() == "hello"
    assert queue.empty()


async def test_feed_queue_no_trailing_newline():
    """A line without trailing newline is not enqueued (readline only returns complete lines)."""
    reader = asyncio.StreamReader()
    reader.feed_data(b"complete\npartial")
    # No EOF yet — partial line is buffered in the reader, not yet yielded by readline()

    queue: asyncio.Queue[str] = asyncio.Queue()
    feed_task = asyncio.create_task(_feed_queue(reader, queue))
    await asyncio.sleep(0)

    # Only the complete line should be in the queue
    assert queue.get_nowait() == "complete"
    assert queue.empty()
    assert not feed_task.done()

    feed_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await feed_task


async def test_session_drain_bg_output(tmp_path: Path) -> None:
    session_id = "test-bg-drain"
    home = tmp_path / "home"
    home.mkdir()
    paths = SessionPaths(session_id=session_id, home=home, xdg_cache_home=home / "cache")
    session = Session(session_id=session_id, paths=paths, profile=TEST_PROFILE)

    q: asyncio.Queue[str] = asyncio.Queue()
    q.put_nowait("hello")
    q.put_nowait("world")
    session.add_bg_source("mytask", BgStream.STDOUT, q)

    result = session.drain_bg_output()
    assert result == {("mytask", BgStream.STDOUT): ["hello", "world"]}
    assert session.drain_bg_output() == {}  # second drain is empty


async def test_session_drain_bg_output_skips_empty_queues(tmp_path: Path) -> None:
    """drain_bg_output omits (task, stream) keys with no lines."""
    session_id = "test-bg-empty"
    home = tmp_path / "home"
    home.mkdir()
    paths = SessionPaths(session_id=session_id, home=home, xdg_cache_home=home / "cache")
    session = Session(session_id=session_id, paths=paths, profile=TEST_PROFILE)

    empty_q: asyncio.Queue[str] = asyncio.Queue()
    session.add_bg_source("mytask", BgStream.STDOUT, empty_q)

    assert session.drain_bg_output() == {}


if __name__ == "__main__":
    pytest_bazel.main()
