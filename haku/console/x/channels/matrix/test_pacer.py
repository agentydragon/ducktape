"""What the room's outbound queue guarantees: order, a rate, and one collapsing slot."""

from __future__ import annotations

import asyncio

import pytest
import pytest_bazel

from haku.console.x.channels.matrix.client import MatrixError
from haku.console.x.channels.matrix.pacer import MAX_QUEUED_SENDS, RoomPacer, Send


def recorder(sent: list[str], label: str) -> Send:
    async def send() -> None:
        sent.append(label)

    return send


async def test_sends_go_out_in_the_order_they_were_queued() -> None:
    """FIFO, because most of what the console says loses information when reordered: a
    bootstrap narration read out of sequence describes a different bootstrap."""
    sent: list[str] = []
    pacer = RoomPacer(sends_per_second=1e6, burst=100)

    async with pacer.run():
        for label in "abcde":
            pacer.send(recorder(sent, label))
        await pacer.flush()

    assert sent == ["a", "b", "c", "d", "e"]


async def test_a_burst_beyond_the_budget_is_spread_rather_than_dropped() -> None:
    """The property the queue exists for: everything arrives, just not at once.

    Sending faster than `rc_message` earns a 429, and a 429 nio absorbs silently is a send that
    never returns — so the fix is not making them rather than handling them.
    """
    sent: list[str] = []
    # Two immediately, then one per 10ms: the shape of Synapse's bucket, at a rate a test
    # can wait for.
    pacer = RoomPacer(sends_per_second=100.0, burst=2)

    async with pacer.run():
        started = asyncio.get_running_loop().time()
        for label in "abcde":
            pacer.send(recorder(sent, label))
        await pacer.flush()
        elapsed = asyncio.get_running_loop().time() - started

    assert sent == ["a", "b", "c", "d", "e"]
    # Three sends past the burst, each waiting out one 10ms refill.
    assert elapsed >= 0.03, f"the burst was not paced ({elapsed=})"


async def test_a_revision_replaces_one_of_its_own_that_has_not_gone_out() -> None:
    """The one sender allowed to overwrite itself: nobody needs the tool call a line showed
    four edits ago, and spending the room's budget on states already superseded is what leaves
    the *current* one waiting."""
    sent: list[str] = []
    pacer = RoomPacer(sends_per_second=100.0, burst=1)

    async with pacer.run():
        pacer.send(recorder(sent, "answer"))  # takes the one token
        pacer.revise("turn:1", recorder(sent, "running Bash"))
        pacer.revise("turn:1", recorder(sent, "running Read"))
        pacer.revise("turn:1", recorder(sent, "running Grep"))
        await pacer.flush()

    assert sent == ["answer", "running Grep"]


async def test_each_subject_collapses_alone() -> None:
    """Two spans are two lines: a session line's change must not eat a turn line's, however both
    squash their own."""
    sent: list[str] = []
    pacer = RoomPacer(sends_per_second=100.0, burst=1)

    async with pacer.run():
        pacer.send(recorder(sent, "answer"))  # takes the one token
        pacer.revise("session:1", recorder(sent, "provisioning"))
        pacer.revise("turn:4", recorder(sent, "running Bash"))
        pacer.revise("session:1", recorder(sent, "cloning haku-state"))
        await pacer.flush()

    assert sent == ["answer", "cloning haku-state", "running Bash"]


async def test_a_revision_keeps_the_place_it_was_first_given() -> None:
    """Collapsing is not jumping the queue: a line that keeps overwriting itself must not starve
    the answer queued behind it, nor arrive ahead of the notice queued before it."""
    sent: list[str] = []
    pacer = RoomPacer(sends_per_second=100.0, burst=1)

    async with pacer.run():
        pacer.send(recorder(sent, "first"))  # takes the one token
        pacer.revise("turn:1", recorder(sent, "status one"))
        pacer.send(recorder(sent, "second"))
        pacer.revise("turn:1", recorder(sent, "status two"))
        await pacer.flush()

    assert sent == ["first", "status two", "second"]


async def test_retiring_a_line_drops_a_change_that_never_went_out() -> None:
    """A create-then-immediately-redact spends two of the room's ten sends to show something
    for a fraction of a second."""
    sent: list[str] = []
    pacer = RoomPacer(sends_per_second=100.0, burst=1)

    async with pacer.run():
        pacer.send(recorder(sent, "answer"))  # takes the one token
        pacer.revise("turn:1", recorder(sent, "running Bash"))
        pacer.drop("turn:1")
        await pacer.flush()

    assert sent == ["answer"]


async def test_one_failed_send_does_not_stop_the_queue() -> None:
    """A room that cannot be spoken to is not a reason to stop trying, and never a reason to
    end the conversation happening behind it."""
    sent: list[str] = []
    pacer = RoomPacer(sends_per_second=1e6, burst=100)

    async def explode() -> None:
        raise MatrixError("M_FORBIDDEN: nope")

    async with pacer.run():
        pacer.send(recorder(sent, "before"))
        pacer.send(explode)
        pacer.send(recorder(sent, "after"))
        await pacer.flush()

    assert sent == ["before", "after"]


async def test_a_required_send_reports_failure_without_stopping_the_queue() -> None:
    sent: list[str] = []
    pacer = RoomPacer(sends_per_second=1e6, burst=100)

    async def explode() -> None:
        raise MatrixError("M_FORBIDDEN: nope")

    async with pacer.run():
        with pytest.raises(MatrixError, match="M_FORBIDDEN"):
            await pacer.send_and_wait(explode)
        pacer.send(recorder(sent, "after"))
        await pacer.flush()

    assert sent == ["after"]


async def test_a_429_is_believed_over_our_own_accounting() -> None:
    """The homeserver's `retry_after_ms` is the only real measurement this console gets of a budget
    it otherwise only estimates: two replicas can each think they own the whole of it.
    """
    sent: list[str] = []
    pacer = RoomPacer(sends_per_second=1e6, burst=100)

    async def limited() -> None:
        raise MatrixError("M_LIMIT_EXCEEDED: slow down", retry_after_ms=40)

    async with pacer.run():
        started = asyncio.get_running_loop().time()
        pacer.send(limited)
        pacer.send(recorder(sent, "after"))
        await pacer.flush()
        elapsed = asyncio.get_running_loop().time() - started

    assert sent == ["after"]
    assert elapsed >= 0.04, f"the server's retry_after_ms was ignored ({elapsed=})"


async def test_a_room_nobody_is_draining_stops_growing(caplog: pytest.LogCaptureFixture) -> None:
    """The queue is a buffer, not a store. Reaching the cap means the room has been unreachable
    for a long time, so it is worth saying out loud rather than absorbing."""
    sent: list[str] = []
    pacer = RoomPacer()  # never run, so nothing drains — a room that has gone away

    for index in range(MAX_QUEUED_SENDS + 5):
        pacer.send(recorder(sent, str(index)))

    assert "dropping one" in caplog.text


if __name__ == "__main__":
    pytest_bazel.main()
