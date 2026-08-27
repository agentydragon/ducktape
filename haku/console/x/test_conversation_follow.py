"""Following a conversation, against a real Postgres and a real notification channel.

Both ends are the point. The wake is emitted by the write's own transaction and travels a broadcast
channel; what a follower is sent is read back out of the record. Standing either end in would
assert the operation against an imagined shape, which is how the session listener passed every test
it had while raising on every call in production (<README.md> § Tests run against a real database).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from more_itertools import one
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.chat_models import SPA_ORIGIN, BridgeFrameKind, FrameDirection, ItemStatus, ItemType, SessionStatus
from haku.console.x.conftest import attach_channel
from haku.console.x.conversation_events import FrameRange, ItemSegment, MessageCompleted, MessageStarted, OpenRef
from haku.console.x.conversation_follow import ConversationFollow
from haku.console.x.conversation_reads import ConversationEntry, MessageEntry, PromptEntry
from haku.console.x.conversation_views import (
    ConversationFollowMessage,
    ConversationSnapshot,
    ConversationUpdate,
    ConversationView,
)
from haku.console.x.session_events import TurnAnsweredBody
from haku.console.x.session_notifications import SessionNotifications
from haku.console.x.session_runtime import SessionService
from haku.console.x.session_store import SessionStore
from haku.console.x.testing.recording_claims import RecordingClaims

# Long enough that several writes land inside one window on a loaded machine, short enough that
# waiting one out does not try anyone's patience.
WINDOW = timedelta(milliseconds=300)
# The same for the sandbox re-read, which has no wake to arrive on.
SANDBOX_POLL = timedelta(milliseconds=300)
# How long a message is waited for before its absence is called deliberate.
PATIENCE = WINDOW * 8


@pytest.fixture
def following(
    chat_store: SessionStore, chat_service: SessionService, notifications: SessionNotifications
) -> ConversationFollow:
    return ConversationFollow(chat_store, chat_service, notifications, window=WINDOW, sandbox_poll=SANDBOX_POLL)


async def _next(messages: AsyncIterator[ConversationFollowMessage]) -> ConversationFollowMessage:
    async with asyncio.timeout(PATIENCE.total_seconds()):
        return await anext(messages)


async def _nothing_more(messages: AsyncIterator[ConversationFollowMessage]) -> None:
    """An absence can only be established by waiting one out."""
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(PATIENCE.total_seconds()):
            await anext(messages)


def _prose(entries: list[ConversationEntry]) -> list[str]:
    """The spoken texts of a stream, in order — what most follow assertions care about."""
    return [entry.text for entry in entries if isinstance(entry, PromptEntry | MessageEntry)]


async def _started(chat_store: SessionStore, operator_id: UUID) -> tuple[UUID, UUID]:
    view, token = await chat_store.create(operator_id)
    await chat_store.authenticate_bridge(view.session_id, token)
    return view.session_id, await chat_store.conversation_of(view.session_id)


async def _exchange(chat_store: SessionStore, operator_id: UUID, session_id: UUID, prompt: str, answer: str) -> None:
    """One prompt through to one finished answer, with the frames it took, as the loop writes them."""
    await chat_store.enqueue_prompt(operator_id, session_id, prompt, SPA_ORIGIN)
    turn = await chat_store.next_prompt(session_id)
    assert turn is not None
    await chat_store.record_frame(session_id, FrameDirection.TO_AGENT, BridgeFrameKind.HARNESS_FRAME, {"type": "user"})
    spoke = await chat_store.record_frame(
        session_id, FrameDirection.FROM_AGENT, BridgeFrameKind.HARNESS_FRAME, {"type": "assistant"}
    )
    where = FrameRange(spoke.frame_seq, spoke.frame_seq)
    await chat_store.apply_frame(
        session_id,
        turn.turn_id,
        spoke.frame_seq,
        [
            MessageStarted(provenance=where),
            ItemSegment(item=OpenRef(item_type=ItemType.MESSAGE), text=answer, provenance=where),
            MessageCompleted(backend_item_id=None, provenance=where),
        ],
    )
    await chat_store.end_turn(turn.turn_id, TurnAnsweredBody(), last_frame_seq=spoke.frame_seq)


async def test_a_follow_opens_with_the_conversation_whole(
    following: ConversationFollow, chat_store: SessionStore, operator_id: UUID
) -> None:
    """A follower that has never read one is given the state, not an empty stream it must go and
    fill in itself. The position it carries is what everything after it continues from."""
    session_id, conversation_id = await _started(chat_store, operator_id)
    await _exchange(chat_store, operator_id, session_id, "first", "one")

    opened = await _next(following.follow(operator_id, conversation_id))

    assert isinstance(opened, ConversationSnapshot)
    assert _prose(opened.conversation.entries) == ["first", "one"]
    assert opened.position == await chat_store.conversation_position(conversation_id)


async def test_what_moves_after_the_snapshot_arrives_as_an_update(
    following: ConversationFollow, chat_store: SessionStore, operator_id: UUID
) -> None:
    """What an update drops is the history — the part that grows without bound."""
    session_id, conversation_id = await _started(chat_store, operator_id)
    await _exchange(chat_store, operator_id, session_id, "first", "one")
    messages = following.follow(operator_id, conversation_id)
    assert isinstance(await _next(messages), ConversationSnapshot)

    await _exchange(chat_store, operator_id, session_id, "second", "two")
    update = await _next(messages)

    assert isinstance(update, ConversationUpdate)
    assert _prose(update.entries) == ["second", "two"]
    await messages.aclose()


async def test_a_change_landing_during_the_snapshot_is_carried_by_the_update_after_it(
    following: ConversationFollow, chat_store: SessionStore, operator_id: UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window every list-then-watch client gets wrong, closed here once.

    The position is taken before the state is read, so a row written between the two is newer than
    the position the follower leaves with — and reaches it next, rather than falling in the gap.
    """
    session_id, conversation_id = await _started(chat_store, operator_id)
    whole = chat_store.get_operator_conversation

    async def write_while_reading(operator: UUID, conversation: UUID) -> ConversationView:
        await chat_store.enqueue_prompt(operator_id, session_id, "written mid-read", SPA_ORIGIN)
        return await whole(operator, conversation)

    monkeypatch.setattr(chat_store, "get_operator_conversation", write_while_reading)
    messages = following.follow(operator_id, conversation_id)
    snapshot = await _next(messages)
    monkeypatch.undo()

    assert isinstance(snapshot, ConversationSnapshot)
    update = await _next(messages)
    assert isinstance(update, ConversationUpdate)
    assert _prose(update.entries) == ["written mid-read"]
    await messages.aclose()


async def test_a_resume_is_told_what_it_missed_at_once(
    following: ConversationFollow, chat_store: SessionStore, operator_id: UUID
) -> None:
    """A reconnect is the same operation with the position the last message carried, so what
    happened while the socket was down arrives without waiting for the next thing to happen."""
    session_id, conversation_id = await _started(chat_store, operator_id)
    await _exchange(chat_store, operator_id, session_id, "first", "one")
    held = await chat_store.conversation_position(conversation_id)
    await _exchange(chat_store, operator_id, session_id, "second", "two")

    resumed = await _next(following.follow(operator_id, conversation_id, after=held))

    assert isinstance(resumed, ConversationUpdate)
    assert _prose(resumed.entries) == ["second", "two"]


async def test_a_position_the_log_cannot_answer_from_is_answered_with_the_conversation_whole(
    following: ConversationFollow, chat_store: SessionStore, operator_id: UUID
) -> None:
    """Snapshot-or-resume is the server's decision, which is why a client has no repair path to get
    wrong: an unusable position is not an error it has to recognise and recover from."""
    session_id, conversation_id = await _started(chat_store, operator_id)
    await _exchange(chat_store, operator_id, session_id, "first", "one")
    beyond = await chat_store.conversation_position(conversation_id) + 1_000

    opened = await _next(following.follow(operator_id, conversation_id, after=beyond))

    assert isinstance(opened, ConversationSnapshot)
    assert _prose(opened.conversation.entries) == ["first", "one"]


async def test_a_streaming_turns_segments_become_one_update(
    following: ConversationFollow, chat_store: SessionStore, operator_id: UUID
) -> None:
    """An item's `text` is rewritten in place as its segments land, so every update re-sends the
    open item whole. Coalescing is what keeps that from costing bytes quadratic in the answer's
    length — and the follower still lands on the prose so far, marked as still being written."""
    session_id, conversation_id = await _started(chat_store, operator_id)
    await chat_store.enqueue_prompt(operator_id, session_id, "explain", SPA_ORIGIN)
    turn = await chat_store.next_prompt(session_id)
    assert turn is not None
    messages = following.follow(operator_id, conversation_id)
    assert isinstance(await _next(messages), ConversationSnapshot)

    where = FrameRange(1, 1)
    await chat_store.apply_frame(session_id, turn.turn_id, 1, [MessageStarted(provenance=where)])
    for seq, word in enumerate(("the ", "answer ", "so ", "far"), start=2):
        await chat_store.apply_frame(
            session_id,
            turn.turn_id,
            seq,
            [ItemSegment(item=OpenRef(item_type=ItemType.MESSAGE), text=word, provenance=FrameRange(seq, seq))],
        )
    update = await _next(messages)

    assert isinstance(update, ConversationUpdate)
    writing = one(entry for entry in update.entries if isinstance(entry, MessageEntry))
    assert (writing.status, writing.text) == (ItemStatus.OPEN, "the answer so far")
    assert update.status == SessionStatus.RESPONDING
    await _nothing_more(messages)
    await messages.aclose()


async def test_a_replacement_sessions_rows_reach_a_follower_that_never_named_it(
    following: ConversationFollow, chat_store: SessionStore, operator_id: UUID
) -> None:
    """What addressing the thread rather than the runner buys. A session lives only as long as its
    sandbox, so a follower that had to name one would be reading a dead log after every replacement.
    """
    first, conversation_id = await _started(chat_store, operator_id)
    messages = following.follow(operator_id, conversation_id)
    assert isinstance(await _next(messages), ConversationSnapshot)

    replacement, token = await chat_store.create(operator_id, conversation_id=conversation_id)
    await chat_store.authenticate_bridge(replacement.session_id, token)
    await _exchange(chat_store, operator_id, replacement.session_id, "carry on", "carrying on")
    update = await _next(messages)

    assert isinstance(update, ConversationUpdate)
    assert update.session_id == replacement.session_id
    assert _prose(update.entries) == ["carry on", "carrying on"]
    # And the session it replaced is now one of the thread's earlier ones, which a follower is told
    # rather than left to infer from a `session_id` it does not recognise.
    assert [earlier.session_id for earlier in update.earlier_sessions] == [first]
    # The whole of the live session's row, so nothing a follower holds still describes the session
    # it has just been told was replaced.
    assert update.created_at == replacement.created_at
    await messages.aclose()


async def test_what_the_sandbox_says_while_coming_up_reaches_a_follower_when_it_is_said(
    following: ConversationFollow, chat_store: SessionStore, operator_id: UUID
) -> None:
    """A session that dies during setup has narration instead of a transcript, so it cannot wait
    for the next event: there will not be one."""
    session_id, conversation_id = await _started(chat_store, operator_id)
    messages = following.follow(operator_id, conversation_id)
    assert isinstance(await _next(messages), ConversationSnapshot)

    await chat_store.narrate(session_id, "pulling the sandbox image")
    update = await _next(messages)

    assert isinstance(update, ConversationUpdate)
    assert [line.text for line in update.narration] == ["pulling the sandbox image"]
    await messages.aclose()


async def test_a_follower_is_not_woken_by_another_conversation(
    following: ConversationFollow, chat_store: SessionStore, operator_id: UUID
) -> None:
    """The wake channel is broadcast, so every replica hears every session: what keeps a follower to
    its own thread is this replica's own routing, not what it was told."""
    _, conversation_id = await _started(chat_store, operator_id)
    elsewhere, _ = await _started(chat_store, operator_id)
    messages = following.follow(operator_id, conversation_id)
    assert isinstance(await _next(messages), ConversationSnapshot)

    await _exchange(chat_store, operator_id, elsewhere, "not yours", "indeed not")

    await _nothing_more(messages)
    await messages.aclose()


async def test_a_conversation_another_operator_owns_cannot_be_followed(
    following: ConversationFollow, chat_store: SessionStore, operator_id: UUID
) -> None:
    """The socket is operator-scoped by being the store's own read; a follow can see exactly what a
    read of the same conversation can, and nothing more."""
    _, conversation_id = await _started(chat_store, operator_id)

    with pytest.raises(KeyError):
        await _next(following.follow(uuid4(), conversation_id))


async def test_an_update_carries_the_channels_holding_the_conversation(
    following: ConversationFollow,
    chat_store: SessionStore,
    operator_id: UUID,
    migrated_sessions: async_sessionmaker[AsyncSession],
) -> None:
    """A tab watching a thread a room later joins would otherwise show an attachment list from
    whenever it connected — a UI disagreeing with the database until someone reloads it."""
    session_id, conversation_id = await _started(chat_store, operator_id)
    messages = following.follow(operator_id, conversation_id)
    opened = await _next(messages)
    assert isinstance(opened, ConversationSnapshot)
    assert opened.conversation.attachments == []

    await attach_channel(migrated_sessions, session_id, "!room:example.org")
    await _exchange(chat_store, operator_id, session_id, "hello from the room", "hello back")
    update = await _next(messages)

    assert isinstance(update, ConversationUpdate)
    assert [attachment.address for attachment in update.attachments] == ["!room:example.org"]
    await messages.aclose()


async def test_a_sandbox_still_coming_up_is_read_again_with_no_wake_to_carry_it(
    following: ConversationFollow, chat_store: SessionStore, operator_id: UUID, recording_claims: RecordingClaims
) -> None:
    """Kubernetes writes no `session_events` row when a pod goes ready, so a follower waiting only
    for wakes would show a provisioning panel frozen at whatever it opened on — during exactly the
    phase that panel exists for."""
    view, _ = await chat_store.create(operator_id)
    conversation_id = await chat_store.conversation_of(view.session_id)
    messages = following.follow(operator_id, conversation_id)

    opened = await _next(messages)
    assert isinstance(opened, ConversationSnapshot)
    assert opened.conversation.session.status == SessionStatus.PROVISIONING
    assert opened.conversation.session.provisioning is not None

    # Nothing is written between these two, and the second still arrives.
    polled = await _next(messages)
    assert isinstance(polled, ConversationUpdate)
    assert polled.provisioning is not None
    assert recording_claims.inspected, "the cluster is what a provisioning view is read from"
    await messages.aclose()


async def test_a_session_past_provisioning_is_not_polled(
    following: ConversationFollow, chat_store: SessionStore, operator_id: UUID, recording_claims: RecordingClaims
) -> None:
    """The poll is for the one field whose truth lives outside the log, and stops being paid for
    the moment that field is `None`: a live transcript must not put a cluster read on its hot path.
    """
    _, conversation_id = await _started(chat_store, operator_id)
    messages = following.follow(operator_id, conversation_id)
    opened = await _next(messages)
    assert isinstance(opened, ConversationSnapshot)
    assert opened.conversation.session.provisioning is None

    await _nothing_more(messages)

    assert recording_claims.inspected == []
    await messages.aclose()


if __name__ == "__main__":
    pytest_bazel.main()
