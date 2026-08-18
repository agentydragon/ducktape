"""Contracts of a subscription: what a position addresses, and who keeps it.

No channel is imported here. A durable cursor lives beneath a channel boundary and is tested there
(<channels/matrix/test_room_subscription.py>); this file holds the stream itself and the
client-held position, which every consumer shares.

The events are the operator's own prompts, because `prompt_enqueued` is the one kind a test can
write without a runner.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import pytest
import pytest_bazel

from haku.console.chat_models import SPA_ORIGIN, TurnOutcome
from haku.console.x.session_events import PromptBody
from haku.console.x.session_store import SessionStore
from haku.console.x.subscription import (
    START,
    Backlog,
    ClientHeldCursor,
    ConversationStream,
    Read,
    StreamPosition,
    Subscription,
)


@dataclass(frozen=True)
class Thread:
    """A conversation and the session currently writing into it."""

    conversation_id: UUID
    session_id: UUID


@pytest.fixture
def stream(migrated_sessions) -> ConversationStream:
    return ConversationStream(migrated_sessions)


async def a_thread(chat_store: SessionStore, operator_id: UUID, *said: str) -> Thread:
    """A ready session whose conversation holds one `prompt_enqueued` event per prompt."""
    view, token = await chat_store.create(operator_id)
    await chat_store.authenticate_bridge(view.session_id, token)
    thread = Thread(conversation_id=await chat_store.conversation_of(view.session_id), session_id=view.session_id)
    for prompt in said:
        await say(chat_store, operator_id, thread, prompt)
    return thread


async def say(chat_store: SessionStore, operator_id: UUID, thread: Thread, prompt: str) -> None:
    """Enqueue one prompt and let a turn claim it.

    Admission refuses a second prompt while one is queued, so each has to be claimed before the
    next is accepted; the turn ends answered, which records nothing of its own.
    """
    await chat_store.enqueue_prompt(operator_id, thread.session_id, prompt, SPA_ORIGIN)
    turn = await chat_store.next_prompt(thread.session_id)
    assert turn is not None
    await chat_store.end_turn(turn.turn_id, TurnOutcome.ANSWERED)


def prompts(read: Read) -> list[str]:
    # Never `Unstarted`: a client-held position is always a position, even when it is `START`.
    assert isinstance(read, Backlog)
    return [event.body.text for event in read.events if isinstance(event.body, PromptBody)]


async def test_two_subscribers_at_different_positions_read_only_what_each_has_not_seen(
    chat_store, operator_id, stream
) -> None:
    """One conversation, two readers, no agreement needed between them and nothing stored about
    either."""
    thread = await a_thread(chat_store, operator_id, "one", "two", "three")
    whole = await stream.read(thread.conversation_id, after=START)

    behind = Subscription(stream, ClientHeldCursor(START), thread.conversation_id)
    ahead = Subscription(stream, ClientHeldCursor(whole.events[0].position), thread.conversation_id)

    assert prompts(await behind.read()) == ["one", "two", "three"]
    assert prompts(await ahead.read()) == ["two", "three"]


async def test_a_client_held_subscriber_resumes_from_whatever_position_it_sends(
    chat_store, operator_id, stream
) -> None:
    """A tab's position survives nothing but the tab, so a reload reads from wherever its next
    request says — including from the start, which is the whole transcript and not an error."""
    thread = await a_thread(chat_store, operator_id, "one", "two")
    caught_up = await stream.read(thread.conversation_id, after=START)

    resumed = Subscription(stream, ClientHeldCursor(caught_up.position), thread.conversation_id)
    assert prompts(await resumed.read()) == []

    await say(chat_store, operator_id, thread, "three")
    assert prompts(await resumed.read()) == ["three"]


async def test_keeping_a_client_held_position_stores_nothing(chat_store, operator_id, stream) -> None:
    """`keep` is where a durable subscriber writes its row; this one has nowhere to write it, which
    is what makes several tabs on one conversation cost the console no state at all."""
    thread = await a_thread(chat_store, operator_id, "one")
    subscription = Subscription(stream, ClientHeldCursor(START), thread.conversation_id)
    read = await subscription.read()
    assert isinstance(read, Backlog)
    await subscription.keep(read.position)

    assert prompts(await subscription.read()) == ["one"]


async def test_a_gap_in_event_seq_is_not_a_loss(chat_store, operator_id, stream) -> None:
    """`event_seq` is global, so another conversation writing between two of ours leaves our rows
    non-contiguous. Every read is "everything after N", so the hole is not even visible."""
    ours = await a_thread(chat_store, operator_id, "first")
    theirs = await a_thread(chat_store, operator_id, "not ours")
    await say(chat_store, operator_id, ours, "second")

    read = await stream.read(ours.conversation_id, after=START)

    assert prompts(read) == ["first", "second"]
    seqs = [event.position.event_seq for event in read.events]
    assert seqs[1] > seqs[0] + 1, f"expected the other conversation to have taken a sequence value: {seqs=}"
    assert prompts(await stream.read(theirs.conversation_id, after=START)) == ["not ours"]


async def test_a_position_stays_put_when_nothing_has_moved(chat_store, operator_id, stream) -> None:
    """An empty read hands back the position it was asked from, never the head — so keeping it
    cannot carry a subscriber past a row committed between the read and the keep."""
    thread = await a_thread(chat_store, operator_id, "one")
    caught_up = await stream.read(thread.conversation_id, after=START)

    again = await stream.read(thread.conversation_id, after=caught_up.position)

    assert (again.events, again.position, again.more) == ((), caught_up.position, False)


async def test_a_read_that_stops_at_its_limit_says_there_is_more(chat_store, operator_id, stream) -> None:
    thread = await a_thread(chat_store, operator_id, "one", "two", "three")

    first = await stream.read(thread.conversation_id, after=START, limit=1)
    assert (prompts(first), first.more) == (["one"], True)

    rest = await stream.read(thread.conversation_id, after=first.position, limit=10)
    assert (prompts(rest), rest.more) == (["two", "three"], False)


async def test_a_replacement_session_continues_the_same_stream(chat_store, operator_id, stream) -> None:
    """The stream is keyed by the thread, so a subscriber holding a position from before the sandbox
    died reads on into its replacement's rows."""
    thread = await a_thread(chat_store, operator_id, "before")
    replacement, token = await chat_store.create(operator_id, conversation_id=thread.conversation_id)
    await chat_store.authenticate_bridge(replacement.session_id, token)
    await chat_store.enqueue_prompt(operator_id, replacement.session_id, "after", SPA_ORIGIN)

    assert prompts(await stream.read(thread.conversation_id, after=START)) == ["before", "after"]


async def test_the_head_of_a_conversation_nothing_has_been_said_in_is_the_start(
    chat_store, operator_id, stream
) -> None:
    """Zero is a position no row can carry, so "nothing recorded yet" and "read everything" are one
    number rather than two states."""
    view, _ = await chat_store.create(operator_id)

    assert await stream.head(await chat_store.conversation_of(view.session_id)) == START


async def test_the_head_is_where_a_caught_up_reader_would_be(chat_store, operator_id, stream) -> None:
    thread = await a_thread(chat_store, operator_id, "one", "two")

    read = await stream.read(thread.conversation_id, after=START)

    assert await stream.head(thread.conversation_id) == read.position
    assert read.position > StreamPosition(event_seq=0)


if __name__ == "__main__":
    pytest_bazel.main()
