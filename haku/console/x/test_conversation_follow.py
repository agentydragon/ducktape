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

from haku.console.chat_models import ChatMessageStatus, FrameDirection, SessionStatus, TurnOutcome
from haku.console.x.claude_code.frames import PROMPT_FRAME_KIND
from haku.console.x.conversation_events import FrameRange, MessageCompleted, MessageKey
from haku.console.x.conversation_follow import ConversationFollow
from haku.console.x.session_notifications import SessionNotifications
from haku.console.x.session_store import SessionStore, SpaSession
from haku.console.x.session_views import (
    ConversationFollowMessage,
    ConversationSnapshot,
    ConversationUpdate,
    ConversationView,
)

# Long enough that several writes land inside one window on a loaded machine, short enough that
# waiting one out does not try anyone's patience.
WINDOW = timedelta(milliseconds=300)
# How long a message is waited for before its absence is called deliberate.
PATIENCE = WINDOW * 8


@pytest.fixture
async def following(chat_store: SessionStore, notifications: SessionNotifications) -> AsyncIterator[ConversationFollow]:
    follow = ConversationFollow(chat_store, notifications, window=WINDOW)
    async with follow.run():
        yield follow


async def _next(messages: AsyncIterator[ConversationFollowMessage]) -> ConversationFollowMessage:
    async with asyncio.timeout(PATIENCE.total_seconds()):
        return await anext(messages)


async def _nothing_more(messages: AsyncIterator[ConversationFollowMessage]) -> None:
    """An absence can only be established by waiting one out."""
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(PATIENCE.total_seconds()):
            await anext(messages)


async def _started(chat_store: SessionStore, operator_id: UUID) -> tuple[UUID, UUID]:
    view, token = await chat_store.create(operator_id, SpaSession())
    await chat_store.authenticate_bridge(view.session_id, token)
    return view.session_id, await chat_store.conversation_of(view.session_id)


async def _exchange(chat_store: SessionStore, operator_id: UUID, session_id: UUID, prompt: str, answer: str) -> None:
    """One prompt through to one finished answer, with the frames it took, as the loop writes them."""
    await chat_store.enqueue_prompt(operator_id, session_id, prompt)
    turn = await chat_store.next_prompt(session_id)
    assert turn is not None
    sent = await chat_store.record_frame(session_id, FrameDirection.TO_AGENT, PROMPT_FRAME_KIND, {"type": "user"})
    await chat_store.set_message_source_frames(session_id, turn.message_id, sent.frame_seq)
    spoke = await chat_store.record_frame(session_id, FrameDirection.FROM_AGENT, "assistant", {"type": "assistant"})
    await chat_store.apply_frame(
        session_id,
        turn.turn_id,
        spoke.frame_seq,
        [
            MessageCompleted(
                message=MessageKey(opened_at_frame_seq=spoke.frame_seq),
                text=answer,
                agent_message_id=None,
                provenance=FrameRange(spoke.frame_seq, spoke.frame_seq),
            )
        ],
    )
    await chat_store.end_turn(turn.turn_id, TurnOutcome.ANSWERED, last_frame_seq=spoke.frame_seq)


async def test_a_follow_opens_with_the_conversation_whole(
    following: ConversationFollow, chat_store: SessionStore, operator_id: UUID
) -> None:
    """A follower that has never read one is given the state, not an empty stream it must go and
    fill in itself. The position it carries is what everything after it continues from."""
    session_id, conversation_id = await _started(chat_store, operator_id)
    await _exchange(chat_store, operator_id, session_id, "first", "one")

    opened = await _next(following.follow(operator_id, conversation_id))

    assert isinstance(opened, ConversationSnapshot)
    assert [message.content for message in opened.conversation.session.messages] == ["first", "one"]
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
    assert [message.content for message in update.messages] == ["second", "two"]
    assert [turn.outcome for turn in update.turns] == [TurnOutcome.ANSWERED]
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
        await chat_store.enqueue_prompt(operator_id, session_id, "written mid-read")
        return await whole(operator, conversation)

    monkeypatch.setattr(chat_store, "get_operator_conversation", write_while_reading)
    messages = following.follow(operator_id, conversation_id)
    snapshot = await _next(messages)
    monkeypatch.undo()

    assert isinstance(snapshot, ConversationSnapshot)
    update = await _next(messages)
    assert isinstance(update, ConversationUpdate)
    assert [message.content for message in update.messages] == ["written mid-read"]
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
    assert [message.content for message in resumed.messages] == ["second", "two"]


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
    assert [message.content for message in opened.conversation.session.messages] == ["first", "one"]


async def test_a_streaming_turns_deltas_become_one_update(
    following: ConversationFollow, chat_store: SessionStore, operator_id: UUID
) -> None:
    """`content` is rewritten in place per delta and a delta is not a row, so every update re-sends
    the open message whole. Coalescing is what keeps that from costing bytes quadratic in the
    answer's length — and the follower still lands on the prose so far."""
    session_id, conversation_id = await _started(chat_store, operator_id)
    await chat_store.enqueue_prompt(operator_id, session_id, "explain")
    turn = await chat_store.next_prompt(session_id)
    assert turn is not None
    messages = following.follow(operator_id, conversation_id)
    assert isinstance(await _next(messages), ConversationSnapshot)

    message_id = await chat_store.begin_assistant(session_id, turn.turn_id, source_first_frame_seq=1)
    prose = ""
    for word in ("the ", "answer ", "so ", "far"):
        prose += word
        await chat_store.update_assistant(session_id, message_id, prose)
    update = await _next(messages)

    assert isinstance(update, ConversationUpdate)
    assert ("the answer so far", ChatMessageStatus.STREAMING) in [
        (message.content, message.status) for message in update.messages
    ]
    assert update.status == SessionStatus.RESPONDING
    await _nothing_more(messages)
    await messages.aclose()


async def test_a_replacement_sessions_rows_reach_a_follower_that_never_named_it(
    following: ConversationFollow, chat_store: SessionStore, operator_id: UUID
) -> None:
    """What addressing the thread rather than the runner buys. A session lives only as long as its
    sandbox, so a follower that had to name one would be reading a dead log after every replacement.
    """
    _, conversation_id = await _started(chat_store, operator_id)
    messages = following.follow(operator_id, conversation_id)
    assert isinstance(await _next(messages), ConversationSnapshot)

    replacement, token = await chat_store.create(operator_id, SpaSession(), conversation_id=conversation_id)
    await chat_store.authenticate_bridge(replacement.session_id, token)
    await _exchange(chat_store, operator_id, replacement.session_id, "carry on", "carrying on")
    update = await _next(messages)

    assert isinstance(update, ConversationUpdate)
    assert update.session_id == replacement.session_id
    assert [message.content for message in update.messages] == ["carry on", "carrying on"]
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


if __name__ == "__main__":
    pytest_bazel.main()
