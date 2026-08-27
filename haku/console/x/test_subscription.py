"""Contracts of a subscription: what a position addresses, and who keeps it.

No channel is imported here. A durable cursor lives beneath a channel boundary and is tested there
(<channels/matrix/test_conversation_subscriber.py>); this file holds the stream itself and the
client-held position, which every consumer shares.

The events are the operator's own prompts, because a prompt item is the one thing a test can write
without a runner.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import pytest
import pytest_bazel
from more_itertools import one
from sqlalchemy import text

from haku.console.chat_models import SPA_ORIGIN
from haku.console.x.session_events import (
    PromptStartedBody,
    SegmentBody,
    SessionProvisioningBody,
    TurnAnsweredBody,
    UnknownEventBody,
)
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
    """A ready session whose conversation holds one prompt item per prompt."""
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
    await chat_store.end_turn(turn.turn_id, TurnAnsweredBody())


def prompts(read: Read) -> list[str]:
    """What was said, in order. Segments are the only prose in the stream, and in these threads the
    only items with any are the prompts — no turn here produces an answer."""
    # Never `Unstarted`: a client-held position is always a position, even when it is `START`.
    assert isinstance(read, Backlog)
    return [event.body.text for event in read.events if isinstance(event.body, SegmentBody)]


async def test_two_subscribers_at_different_positions_read_only_what_each_has_not_seen(
    chat_store, operator_id, stream
) -> None:
    """One conversation, two readers, no agreement needed between them and nothing stored about
    either."""
    thread = await a_thread(chat_store, operator_id, "one", "two", "three")
    whole = await stream.read(thread.conversation_id, after=START)
    said_one = one(event.position for event in whole.events if getattr(event.body, "text", None) == "one")

    behind = Subscription(stream, ClientHeldCursor(START), thread.conversation_id)
    ahead = Subscription(stream, ClientHeldCursor(said_one), thread.conversation_id)

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


async def test_a_conversations_positions_are_its_own_and_contiguous(chat_store, operator_id, stream) -> None:
    """`event_seq` counts within the conversation, so another thread writing between two of ours
    takes none of our numbers.

    Density is what makes a position an answer rather than a hint: a subscriber reading "everything
    after N" can tell a gap from an end, so a lost row is a fact it can act on instead of one
    nothing could distinguish from silence.
    """
    ours = await a_thread(chat_store, operator_id, "first")
    theirs = await a_thread(chat_store, operator_id, "not ours")
    await say(chat_store, operator_id, ours, "second")

    read = await stream.read(ours.conversation_id, after=START)

    assert prompts(read) == ["first", "second"]
    seqs = [event.position.event_seq for event in read.events]
    assert seqs == list(range(1, len(seqs) + 1)), f"expected a dense run starting at one: {seqs=}"
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
    assert (len(first.events), first.more) == (1, True)

    rest = await stream.read(thread.conversation_id, after=first.position, limit=100)
    assert (prompts(rest), rest.more) == (["one", "two", "three"], False)


async def test_a_replacement_session_continues_the_same_stream(chat_store, operator_id, stream) -> None:
    """The stream is keyed by the thread, so a subscriber holding a position from before the sandbox
    died reads on into its replacement's rows."""
    thread = await a_thread(chat_store, operator_id, "before")
    replacement, token = await chat_store.create(operator_id, conversation_id=thread.conversation_id)
    await chat_store.authenticate_bridge(replacement.session_id, token)
    await chat_store.enqueue_prompt(operator_id, replacement.session_id, "after", SPA_ORIGIN)

    assert prompts(await stream.read(thread.conversation_id, after=START)) == ["before", "after"]


async def test_a_kind_this_release_has_no_words_for_is_read_past_rather_than_raised_on(
    chat_store, operator_id, stream, migrated_sessions
) -> None:
    """The roll this stream is exposed to, staged: the console rolls with `maxUnavailable: 0`, so
    the replica on the previous image reads rows the new one wrote, and this read filters no kind in
    SQL. The kind column carries **no** CHECK, precisely so a newer writer's value lands rather than
    being refused; a kind is inserted exactly as that writer would leave it, and what runs against
    it is the vocabulary this release has.

    The row is read, not skipped: it keeps its position, so the prompts around it are still
    delivered and a subscriber's kept position advances over what it was handed.
    """
    thread = await a_thread(chat_store, operator_id, "before")
    async with migrated_sessions() as db, db.begin():
        await db.execute(
            text(
                "INSERT INTO conversation_event "
                "(conversation_id, event_seq, session_id, kind, provenance, body, created_at) "
                "SELECT :conversation_id, next_event_seq, :session_id, 'provisioning_started', 'authored', "
                "'{}'::jsonb, now() FROM conversation WHERE conversation_id = :conversation_id"
            ),
            {"conversation_id": thread.conversation_id, "session_id": thread.session_id},
        )
        await db.execute(
            text("UPDATE conversation SET next_event_seq = next_event_seq + 1 WHERE conversation_id = :id"),
            {"id": thread.conversation_id},
        )
    await say(chat_store, operator_id, thread, "after")

    read = await stream.read(thread.conversation_id, after=START)

    assert prompts(read) == ["before", "after"]
    assert UnknownEventBody(kind="provisioning_started", body={}) in [event.body for event in read.events]
    assert [type(event.body) for event in read.events].count(PromptStartedBody) == 2


async def test_a_new_conversation_already_records_its_session_provisioning(chat_store, operator_id, stream) -> None:
    """Starting the session is the first durable fact, before an operator or runner says anything."""
    view, _ = await chat_store.create(operator_id)
    conversation_id = await chat_store.conversation_of(view.session_id)

    read = await stream.read(conversation_id, after=START)

    assert [type(event.body) for event in read.events] == [SessionProvisioningBody]
    assert await stream.head(conversation_id) == read.position


async def test_the_head_is_where_a_caught_up_reader_would_be(chat_store, operator_id, stream) -> None:
    thread = await a_thread(chat_store, operator_id, "one", "two")

    read = await stream.read(thread.conversation_id, after=START)

    assert await stream.head(thread.conversation_id) == read.position
    assert read.position > StreamPosition(event_seq=0)


if __name__ == "__main__":
    pytest_bazel.main()
