"""What `conversation.py` does with a room: keep a session running under it, build the prompt that
starts one, read back what the room has been told, and take what is said in it into a turn.

Ingress is here rather than beside the turn loop it feeds: `MatrixTurns.offer` takes homeserver
events and hands them to `enqueue_prompt`, so a test of it is a test of the crossing. The turn
loop's own admission rules are <../../test_session_runtime.py>, where no channel appears at all.
"""

from __future__ import annotations

import datetime
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.chat_models import (
    SPA_ORIGIN,
    AuthoredEventKind,
    ChatMessageRole,
    MatrixOrigin,
    PromptRejection,
    SessionStatus,
    TurnOutcome,
)
from haku.console.database_schema import ChatAttachment, Conversation, Session, SessionEvent, SessionMessage
from haku.console.x.channels.matrix.client import InboundMessage, RoomEventKind, UnmappableEvent
from haku.console.x.channels.matrix.conftest import MATRIX_CONFIG, MATRIX_OPERATOR, MATRIX_ROOM, MATRIX_USER
from haku.console.x.channels.matrix.conversation import (
    NOTHING_SAID,
    MatrixConversationStore,
    MatrixSessionSupervisor,
    MatrixSurface,
    MatrixTurns,
    PromptAccepted,
    PromptRejected,
    RoomTranscript,
)
from haku.console.x.channels.matrix.ingress_ledger import IngressLedger
from haku.console.x.conftest import runtime_config
from haku.console.x.session_events import PromptBody
from haku.console.x.session_notifications import SessionNotifications
from haku.console.x.session_runtime import SessionService
from haku.console.x.session_store import ADOPTION_GRACE, BridgeAuthentication, MatrixSession, SessionStore, SpaSession
from haku.console.x.system_prompt import HistoryMessage, SystemPromptTemplate


async def session_behind_the_room(conversations: MatrixConversationStore) -> UUID | None:
    return await conversations.session_serving()


@pytest.fixture
def announced() -> list[str]:
    """What the supervisor said into the room."""
    return []


@pytest.fixture
def supervisor(
    conversations: MatrixConversationStore,
    chat_store: SessionStore,
    chat_service: SessionService,
    notifications: SessionNotifications,
    migrated_identity_store,
    announced: list[str],
) -> MatrixSessionSupervisor:
    """The supervisor over real stores, with only Kubernetes and the announce sink stood in."""

    async def _announce(body: str) -> None:
        announced.append(body)

    return MatrixSessionSupervisor(
        MATRIX_CONFIG,
        conversations,
        chat_service,
        chat_store,
        notifications,
        migrated_identity_store,
        _announce,
        engine=cast(Any, None),  # only `run()` takes the advisory lock; these drive `supervise_once`
    )


async def test_does_nothing_before_a_room_is_bound(supervisor, recording_claims, announced) -> None:
    """Nothing to serve, and nowhere to say so — provisioning here would be a sandbox nobody can reach."""

    await supervisor.supervise_once()

    assert (recording_claims.created, announced) == ([], [])


async def test_provisions_a_session_for_a_freshly_bound_room(
    supervisor, conversations, operator_id, chat_store, recording_claims, announced
) -> None:
    await conversations.bind_room(MATRIX_ROOM, operator_id)

    await supervisor.supervise_once()

    [session_id] = recording_claims.created
    assert await session_behind_the_room(conversations) == session_id
    assert await chat_store.status(session_id) == SessionStatus.PROVISIONING
    assert "provisioning a sandbox" in announced[0]


async def test_leaves_a_live_session_alone(supervisor, conversations, operator_id, recording_claims) -> None:
    await conversations.bind_room(MATRIX_ROOM, operator_id)
    await supervisor.supervise_once()
    [live] = recording_claims.created

    await supervisor.supervise_once()

    assert recording_claims.created == [live], "a live session was replaced"


async def test_replaces_a_failed_session(
    supervisor, conversations, operator_id, chat_store, recording_claims, announced
) -> None:
    """A dead session over Matrix is invisible — the room would just stop answering."""
    await conversations.bind_room(MATRIX_ROOM, operator_id)
    await supervisor.supervise_once()
    [dead] = recording_claims.created
    await chat_store.fail(dead, "the sandbox went away")

    await supervisor.supervise_once()

    assert len(recording_claims.created) == 2
    assert await session_behind_the_room(conversations) not in (None, dead)
    assert dead in recording_claims.deleted, "the dead session's claim must be swept before a new one is made"
    assert any("ended" in line for line in announced)
    # The status alone says a session died; only the reason says which failure it was, and the
    # room is the one place an operator is looking.
    assert any("the sandbox went away" in line for line in announced)


async def test_which_session_serves_the_room_is_read_off_the_thread(
    supervisor, conversations, operator_id, chat_store, recording_claims
) -> None:
    """Nothing points the room at a session: the answer is the newest session of the conversation
    the room's attachment names, which is what makes replacement invisible to the channel."""
    await conversations.bind_room(MATRIX_ROOM, operator_id)
    await supervisor.supervise_once()
    [first] = recording_claims.created
    await chat_store.fail(first, "the sandbox went away")

    await supervisor.supervise_once()

    [_, second] = recording_claims.created
    assert await session_behind_the_room(conversations) == second


async def test_a_replacement_session_joins_the_room_s_conversation_and_the_attachment_stays_put(
    supervisor, conversations, operator_id, chat_store, recording_claims, migrated_sessions
) -> None:
    """Session replacement is the supervisor's normal job, and the room's attachment is not touched
    by it: the successor joins the thread the attachment names."""
    await conversations.bind_room(MATRIX_ROOM, operator_id)
    await supervisor.supervise_once()
    [first] = recording_claims.created
    await chat_store.fail(first, "the sandbox went away")

    await supervisor.supervise_once()

    async with migrated_sessions() as db:
        threads = {row.session_id: row.conversation_id for row in await db.scalars(select(Session))}
        attachments = (
            await db.execute(select(ChatAttachment.conversation_id, ChatAttachment.address, ChatAttachment.detached_at))
        ).all()
    assert len(threads) == 2, "the failed session was replaced"
    assert len(set(threads.values())) == 1, "both sessions run one conversation"
    assert attachments == [(threads[first], MATRIX_ROOM, None)], "one live attachment, never re-pointed"


async def test_replaces_a_session_whose_replica_stopped_renewing_its_lease(
    supervisor, conversations, operator_id, chat_store, recording_claims, migrated_sessions, announced
) -> None:
    """A replica that went away without recording anything leaves a live status nothing is working
    on, and supervision has to reclaim it rather than believe it — but only once the lease has been
    adoptable for a whole `ADOPTION_GRACE` and no runner took it, which is what makes a console
    roll survivable rather than fatal.
    """
    await conversations.bind_room(MATRIX_ROOM, operator_id)
    await supervisor.supervise_once()
    [orphan] = recording_claims.created
    async with migrated_sessions.begin() as db:
        chat = await db.get(Session, orphan)
        assert chat is not None
        chat.lease_expires_at = datetime.datetime.now(datetime.UTC) - ADOPTION_GRACE - datetime.timedelta(seconds=1)

    await supervisor.supervise_once()

    assert len(recording_claims.created) == 2, "the orphaned session was believed rather than replaced"
    assert await session_behind_the_room(conversations) not in (None, orphan)
    assert any("ended" in line for line in announced)


async def test_replaces_a_session_whose_row_is_gone(
    supervisor, conversations, operator_id, recording_claims, migrated_sessions
) -> None:
    """A deleted session leaves the room's thread unserved, and the next pass re-provisions.

    What the schema allows is the session row being deleted underneath the conversation, which
    leaves it with no session rather than a dangling reference.
    """
    await conversations.bind_room(MATRIX_ROOM, operator_id)
    await supervisor.supervise_once()
    [vanished] = recording_claims.created

    async with migrated_sessions.begin() as db:
        await db.execute(delete(Session).where(Session.session_id == vanished))
    assert await session_behind_the_room(conversations) is None, "the thread should be left with no session"

    await supervisor.supervise_once()

    assert len(recording_claims.created) == 2
    assert await session_behind_the_room(conversations) not in (None, vanished)


async def test_does_not_repeat_an_unchanged_status(
    supervisor, conversations, operator_id, chat_store, recording_claims, announced
) -> None:
    """Every transition is reported, but a poll that changes nothing must not spam the room."""
    await conversations.bind_room(MATRIX_ROOM, operator_id)
    await supervisor.supervise_once()
    [session_id] = recording_claims.created
    # Provisioning already announced itself; the runner connecting is the next transition.
    assert (
        await chat_store.authenticate_bridge(session_id, recording_claims.tokens[session_id])
        == BridgeAuthentication.ACCEPTED
    )
    announced.clear()

    await supervisor.supervise_once()
    await supervisor.supervise_once()

    assert announced == [f"session {session_id} is ready"]


@pytest.fixture
async def bound(conversations: MatrixConversationStore, chat_store: SessionStore, operator_id: UUID) -> UUID:
    """A bound room, and a real session row for the prompt to be built for."""
    await conversations.bind_room(MATRIX_ROOM, operator_id)
    view, _ = await chat_store.create(operator_id, SpaSession())
    return view.session_id


# What `RoomChannel.recent_history` is, as a callable the fake room can be handed.
RecentHistory = Callable[[UUID, int], Awaitable[Sequence[HistoryMessage]]]


class _RecordingRoom:
    """A `RoomChannel` that keeps what it was told instead of speaking to a homeserver."""

    def __init__(self, history: RecentHistory) -> None:
        self._history = history
        self.room_id: str | None = MATRIX_ROOM
        self.shown: list[str] = []
        self.cleared = 0
        self.typing: list[bool] = []
        self.announced: list[tuple[str, RoomEventKind]] = []

    async def bound_room(self) -> str | None:
        return self.room_id

    async def recent_history(self, before_session: UUID, limit: int) -> Sequence[HistoryMessage]:
        return await self._history(before_session, limit)

    async def announce(self, body: str, kind: RoomEventKind = RoomEventKind.LIFECYCLE) -> None:
        self.announced.append((body, kind))

    async def show_status(self, body: str, session_id: UUID | None = None) -> None:
        self.shown.append(body)

    async def clear_status(self) -> None:
        self.cleared += 1

    async def set_typing(self, active: bool) -> None:
        self.typing.append(active)


def surface_and_room(history: RecentHistory) -> tuple[MatrixSurface, _RecordingRoom]:
    room = _RecordingRoom(history)
    template = SystemPromptTemplate("{{ room_id }} {{ session_id }} {{ recent_messages | length }}")
    return MatrixSurface(MATRIX_CONFIG, runtime_config(), template, room), room


def surface(history: RecentHistory) -> MatrixSurface:
    return surface_and_room(history)[0]


def served(*messages: HistoryMessage) -> RecentHistory:
    async def _history(before_session: UUID, limit: int) -> tuple[HistoryMessage, ...]:
        assert limit > 0
        return messages

    return _history


def said(sender: str, body: str) -> HistoryMessage:
    return HistoryMessage(sender=sender, body=body, sent_at=datetime.datetime.now(datetime.UTC))


async def test_prompt_describes_the_room_the_channel_is_bound_to(bound: UUID) -> None:
    """No session filtering: being called at all says this session serves the room.

    The console selects this surface from the session's own `surface` column, and the room the
    prompt names comes from the channel, which is the object that knows which room this is.
    """
    assert await surface(served()).system_prompt(bound) == f"{MATRIX_ROOM} {bound} 0"


async def test_a_prompt_cannot_be_built_before_a_room_is_bound(bound: UUID) -> None:
    """A session serving this channel while the channel serves no room is a contradiction, not a
    prompt with the room left out of it."""
    built, room = surface_and_room(served())
    room.room_id = None

    with pytest.raises(RuntimeError):
        await built.system_prompt(bound)


async def test_prompt_survives_a_transcript_that_will_not_answer(bound: UUID) -> None:
    """A read that fails costs the session its context, not its existence."""

    async def unreadable(before_session: UUID, limit: int) -> tuple[HistoryMessage, ...]:
        raise RuntimeError("the database said no")

    assert await surface(unreadable).system_prompt(bound) == f"{MATRIX_ROOM} {bound} 0"


async def test_prompt_carries_both_sides_of_the_conversation(bound: UUID) -> None:
    history = served(said(MATRIX_OPERATOR, "hi"), said(MATRIX_USER, "hello"))

    assert await surface(history).system_prompt(bound) == f"{MATRIX_ROOM} {bound} 2"


async def test_the_history_is_read_for_the_session_being_started(bound: UUID) -> None:
    """Which session is asking is the whole of what excludes its own re-offered prompt."""
    asked: list[UUID] = []

    async def recording(before_session: UUID, limit: int) -> tuple[HistoryMessage, ...]:
        asked.append(before_session)
        return ()

    await surface(recording).system_prompt(bound)

    assert asked == [bound]


async def test_building_a_prompt_says_nothing_into_the_room(bound: UUID) -> None:
    """Reading the room's history is not a reason to post in it."""
    built, room = surface_and_room(served())

    await built.system_prompt(bound)

    assert room.announced == []


async def test_a_turn_with_nothing_to_say_says_so(bound: UUID) -> None:
    """Every turn speaks and there is no silence token: a turn that produced no text would otherwise
    leave the room with nothing at all, which from the operator's side is indistinguishable from a
    lost answer.

    A notice rather than a reply, because nothing was said. Answers do not come through this
    surface at all — they are outbox rows, asserted in `test_outbox.py`.
    """
    del bound
    reporting, room = surface_and_room(served())

    await reporting.report_silent_turn()

    assert room.announced == [(NOTHING_SAID, RoomEventKind.NARRATION)]


@pytest.fixture
def transcript(migrated_sessions) -> RoomTranscript:
    return RoomTranscript(migrated_sessions)


@pytest.fixture
async def thread(conversations: MatrixConversationStore, operator_id: UUID) -> UUID:
    """The conversation the room holds a copy of, opened the way an invite opens it."""
    return (await conversations.bind_room(MATRIX_ROOM, operator_id)).conversation_id


async def another_thread(sessions: async_sessionmaker[AsyncSession], operator_id: UUID) -> UUID:
    """A second conversation, inserted directly rather than bound.

    `bind_room` refuses a second room by design, so a test about two threads cannot ask for one
    through it — which is the point being made below.
    """
    conversation_id = uuid4()
    async with sessions.begin() as db:
        db.add(
            Conversation(
                conversation_id=conversation_id, operator_id=operator_id, created_at=datetime.datetime.now(datetime.UTC)
            )
        )
    return conversation_id


async def serving_session(chat_store: SessionStore, operator_id: UUID, conversation_id: UUID) -> UUID:
    """A Matrix session ready to take prompts, made the way the supervisor and a runner make one."""
    view, token = await chat_store.create(operator_id, MatrixSession(), conversation_id=conversation_id)
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    return view.session_id


async def exchange(chat_store: SessionStore, operator_id: UUID, session_id: UUID, asked: str, answered: str) -> None:
    """One question and its answer, written by the paths that write them in production.

    Not hand-inserted rows: this read depends on the status and content the real writers leave
    behind — a prompt row `pending` until a turn claims it, an assistant row `complete` in the same
    transaction that queues the room's copy.
    """
    await chat_store.enqueue_prompt(operator_id, session_id, asked, SPA_ORIGIN)
    start = await chat_store.next_prompt(session_id)
    assert start is not None
    message_id = await chat_store.begin_assistant(session_id, start.turn_id, source_first_frame_seq=1)
    await chat_store.update_assistant(session_id, message_id, answered, complete=True)
    # Ended, because admission asks about the turn: a session left mid-turn refuses the next
    # prompt, and these tests are conversations rather than one exchange each.
    await chat_store.end_turn(start.turn_id, TurnOutcome.ANSWERED)


async def read(transcript: RoomTranscript, conversation_id: UUID) -> list[tuple[ChatMessageRole, str]]:
    """A thread's recent conversation as a replacement session that owns none of it would read it."""
    return [
        (message.role, message.body)
        for message in await transcript.recent(conversation_id, before_session=uuid4(), limit=20)
    ]


async def test_the_transcript_is_both_sides_of_the_conversation_in_order(
    transcript: RoomTranscript, chat_store: SessionStore, operator_id: UUID, thread: UUID
) -> None:
    session_id = await serving_session(chat_store, operator_id, thread)

    await exchange(chat_store, operator_id, session_id, "hi", "hello")
    await exchange(chat_store, operator_id, session_id, "still there?", "yes")

    assert await read(transcript, thread) == [
        (ChatMessageRole.USER, "hi"),
        (ChatMessageRole.ASSISTANT, "hello"),
        (ChatMessageRole.USER, "still there?"),
        (ChatMessageRole.ASSISTANT, "yes"),
    ]


async def test_the_transcript_spans_every_session_of_the_thread(
    transcript: RoomTranscript, chat_store: SessionStore, operator_id: UUID, thread: UUID
) -> None:
    """The point of reading by conversation: the session that holds the context is the one gone.

    Sessions of one thread share `conversation_id`, so a replacement reads what its predecessor
    said without either of them being named.
    """
    first = await serving_session(chat_store, operator_id, thread)
    await exchange(chat_store, operator_id, first, "hi", "hello")
    await chat_store.fail(first, "the sandbox went away")
    second = await serving_session(chat_store, operator_id, thread)
    await exchange(chat_store, operator_id, second, "again", "still here")

    assert await read(transcript, thread) == [
        (ChatMessageRole.USER, "hi"),
        (ChatMessageRole.ASSISTANT, "hello"),
        (ChatMessageRole.USER, "again"),
        (ChatMessageRole.ASSISTANT, "still here"),
    ]


async def test_a_batch_the_dying_session_never_answered_is_still_the_history(
    transcript: RoomTranscript, chat_store: SessionStore, operator_id: UUID, thread: UUID
) -> None:
    """What answers a message its session never got to: the replacement is handed it as context.

    The batch is acknowledged the moment it is accepted, so nothing offers it again — the prompt
    row ingress wrote is the whole of what survives, and this is the read that finds it.
    """
    doomed = await serving_session(chat_store, operator_id, thread)
    await exchange(chat_store, operator_id, doomed, "hi", "hello")
    # Accepted, and then nothing: no turn ever claimed it, which is what leaves it `pending`.
    await chat_store.enqueue_prompt(operator_id, doomed, "the one that killed it", SPA_ORIGIN)
    await chat_store.fail(doomed, "the sandbox went away")

    assert (ChatMessageRole.USER, "the one that killed it") in await read(transcript, thread)


async def test_a_session_s_own_rows_are_not_its_history(
    transcript: RoomTranscript, chat_store: SessionStore, operator_id: UUID, thread: UUID
) -> None:
    """A prompt this session has already been handed is not also its history; twice is not context.

    The window is real: a session goes `ready` when its runner authenticates, and its system
    prompt is rendered a few statements later — so a batch can be accepted in between.
    """
    doomed = await serving_session(chat_store, operator_id, thread)
    await exchange(chat_store, operator_id, doomed, "hi", "hello")
    replacement = await serving_session(chat_store, operator_id, thread)
    await chat_store.enqueue_prompt(operator_id, replacement, "re-offered", SPA_ORIGIN)

    said = await transcript.recent(thread, before_session=replacement, limit=20)

    assert [(message.role, message.body) for message in said] == [
        (ChatMessageRole.USER, "hi"),
        (ChatMessageRole.ASSISTANT, "hello"),
    ]


async def test_what_the_room_was_never_told_is_not_in_the_history(
    transcript: RoomTranscript, chat_store: SessionStore, operator_id: UUID, thread: UUID
) -> None:
    """Haku's side is here on exactly the condition the room's copy was queued on.

    Two rows exist that no outbox row was ever written for, and neither is context: a message
    still streaming when its session died, and the empty one a message carrying only tool calls
    leaves behind.
    """
    session_id = await serving_session(chat_store, operator_id, thread)
    await chat_store.enqueue_prompt(operator_id, session_id, "do something", SPA_ORIGIN)
    start = await chat_store.next_prompt(session_id)
    assert start is not None
    tool_only = await chat_store.begin_assistant(session_id, start.turn_id, source_first_frame_seq=1)
    await chat_store.update_assistant(session_id, tool_only, "", complete=True)
    streaming = await chat_store.begin_assistant(session_id, start.turn_id, source_first_frame_seq=2)
    await chat_store.update_assistant(session_id, streaming, "half an ans", complete=False)

    assert await read(transcript, thread) == [(ChatMessageRole.USER, "do something")]


async def test_another_thread_is_not_this_thread(
    transcript: RoomTranscript,
    migrated_sessions: async_sessionmaker[AsyncSession],
    chat_store: SessionStore,
    operator_id: UUID,
    thread: UUID,
) -> None:
    """Threads are read apart even though the console services one room at a time, which is what a
    second attached room will need on the day one bot holds several."""
    elsewhere = await serving_session(chat_store, operator_id, await another_thread(migrated_sessions, operator_id))

    await exchange(chat_store, operator_id, elsewhere, "hi", "hello")

    assert await read(transcript, thread) == []


async def test_the_limit_takes_the_tail(
    transcript: RoomTranscript, chat_store: SessionStore, operator_id: UUID, thread: UUID
) -> None:
    session_id = await serving_session(chat_store, operator_id, thread)
    await exchange(chat_store, operator_id, session_id, "one", "re: one")
    await exchange(chat_store, operator_id, session_id, "two", "re: two")

    said = await transcript.recent(thread, before_session=uuid4(), limit=2)

    assert [message.body for message in said] == ["two", "re: two"], "the newest, still oldest first"


@pytest.fixture
def turns(
    conversations: MatrixConversationStore, chat_store: SessionStore, migrated_identity_store, ledger: IngressLedger
) -> MatrixTurns:
    """Ingress over the real stores — only the homeserver's events are handed in by the test."""
    return MatrixTurns(MATRIX_CONFIG, conversations, chat_store, migrated_identity_store, ledger)


async def serving_room(conversations: MatrixConversationStore, operator_id: UUID) -> None:
    """Bind the room, the way an invite does before the supervisor provisions anything.

    Which session then serves it is read off the thread, so binding is all there is to arrange.
    """
    assert (await conversations.bind_room(MATRIX_ROOM, operator_id)).room_id == MATRIX_ROOM


def operator_message(body: str, *, event_id: str, at: int) -> InboundMessage:
    """The operator saying *body* in the room, as `/sync` hands it over."""
    return InboundMessage(
        room_id=MATRIX_ROOM, event_id=event_id, sender=MATRIX_OPERATOR, body=body, origin_server_ts=at
    )


def _unmappable(msgtype: str) -> UnmappableEvent:
    return UnmappableEvent(room_id=MATRIX_ROOM, event_id=f"${msgtype}", sender=MATRIX_OPERATOR, msgtype=msgtype)


async def test_a_batch_a_ready_session_takes_becomes_its_prompt(
    turns: MatrixTurns,
    conversations: MatrixConversationStore,
    chat_store: SessionStore,
    operator_id: UUID,
    thread: UUID,
) -> None:
    """The accepted case, and what "one batch, one prompt" means: two events, one transcript row."""
    session_id = await serving_session(chat_store, operator_id, thread)
    await serving_room(conversations, operator_id)

    admitted = await turns.offer(
        [operator_message("hi", event_id="$1", at=1), operator_message("and this", event_id="$2", at=2)]
    )

    assert isinstance(admitted, PromptAccepted)
    start = await chat_store.next_prompt(session_id)
    assert start is not None
    assert start.prompt == "hi\nand this", "the ids ride on the prompt's own event now, not in its prose"


async def test_a_batch_offered_mid_turn_is_rejected_with_the_reason_and_the_text(
    turns: MatrixTurns,
    conversations: MatrixConversationStore,
    chat_store: SessionStore,
    operator_id: UUID,
    thread: UUID,
) -> None:
    """A message sent while Haku is working is answered rather than queued behind the turn, and the
    row it hands back is the only copy of what was said — the homeserver will not offer it again
    once the caller acknowledges the batch."""
    session_id = await serving_session(chat_store, operator_id, thread)
    await serving_room(conversations, operator_id)
    await chat_store.enqueue_prompt(operator_id, session_id, "first", SPA_ORIGIN)
    assert await chat_store.next_prompt(session_id) is not None

    admitted = await turns.offer([operator_message("and another thing", event_id="$2", at=2)])

    assert isinstance(admitted, PromptRejected)
    assert admitted.reason is PromptRejection.TURN_IN_FLIGHT
    assert admitted.event is not None
    assert admitted.event.session_id == session_id
    assert admitted.event.kind is AuthoredEventKind.PROMPT_REJECTED
    assert admitted.event.body == {"reason": PromptRejection.TURN_IN_FLIGHT, "text": "and another thing"}


async def test_a_batch_offered_before_a_session_exists_is_rejected_with_nothing_to_record(
    turns: MatrixTurns, conversations: MatrixConversationStore, chat_store: SessionStore, operator_id: UUID
) -> None:
    """A room bound before the supervisor has provisioned anything. There is no session to key an
    event to, so the operator's only account of it is the notice."""
    assert (await conversations.bind_room(MATRIX_ROOM, operator_id)).room_id == MATRIX_ROOM

    admitted = await turns.offer([operator_message("hi", event_id="$1", at=1)])

    assert admitted == PromptRejected(reason=PromptRejection.NO_SESSION, event=None)


async def test_a_batch_offered_to_a_session_that_is_gone_is_rejected_rather_than_raised(
    turns: MatrixTurns,
    conversations: MatrixConversationStore,
    chat_store: SessionStore,
    migrated_sessions,
    operator_id: UUID,
    thread: UUID,
) -> None:
    """The supervisor is between sessions, which the room must survive.

    `enqueue_prompt` answers a vanished session with `KeyError`; raising that into the sync loop
    would cost the operator an answer, and it reads as the case above — the row that would carry
    it is the one that has gone.
    """
    session_id = await serving_session(chat_store, operator_id, thread)
    await serving_room(conversations, operator_id)
    async with migrated_sessions.begin() as db:
        await db.execute(delete(Session).where(Session.session_id == session_id))

    admitted = await turns.offer([operator_message("hi", event_id="$1", at=1)])

    assert admitted == PromptRejected(reason=PromptRejection.NO_SESSION, event=None)


async def test_an_accepted_batch_records_its_events_against_the_prompt_it_became(
    turns: MatrixTurns,
    conversations: MatrixConversationStore,
    chat_store: SessionStore,
    ledger: IngressLedger,
    operator_id: UUID,
    thread: UUID,
) -> None:
    """The dedupe key, written where it cannot come apart from the prompt.

    A rejected batch records nothing, because there is no prompt for a row to name and the
    homeserver re-offering it is the outcome we want.
    """
    await serving_session(chat_store, operator_id, thread)
    await serving_room(conversations, operator_id)

    await turns.offer([operator_message("hi", event_id="$1", at=1), operator_message("more", event_id="$2", at=2)])

    assert await ledger.carried(["$1", "$2", "$3"]) == frozenset({"$1", "$2"})


async def test_a_rejected_batch_records_nothing_for_the_homeserver_to_be_deduped_against(
    turns: MatrixTurns,
    conversations: MatrixConversationStore,
    chat_store: SessionStore,
    ledger: IngressLedger,
    operator_id: UUID,
    thread: UUID,
) -> None:
    session_id = await serving_session(chat_store, operator_id, thread)
    await serving_room(conversations, operator_id)
    await chat_store.enqueue_prompt(operator_id, session_id, "first", SPA_ORIGIN)
    assert await chat_store.next_prompt(session_id) is not None

    assert isinstance(await turns.offer([operator_message("hi", event_id="$1", at=1)]), PromptRejected)

    assert await ledger.carried(["$1"]) == frozenset()


async def test_an_unanswered_message_is_asked_again_and_its_events_follow_the_new_prompt(
    turns: MatrixTurns,
    conversations: MatrixConversationStore,
    chat_store: SessionStore,
    ledger: IngressLedger,
    operator_id: UUID,
    thread: UUID,
) -> None:
    """Suppression is not acknowledgement: the batch was acknowledged to the homeserver, so this
    prompt is the only copy left of what the operator asked, and the session holding it died.

    The events move to the replacement's prompt, which is what stops the next pass finding the
    same message outstanding and asking a third time.
    """
    doomed = await serving_session(chat_store, operator_id, thread)
    await serving_room(conversations, operator_id)
    await turns.offer([operator_message("did you see this", event_id="$1", at=1)])
    await chat_store.closed(doomed)
    replacement = await serving_session(chat_store, operator_id, thread)

    unanswered = await ledger.unanswered()
    assert unanswered is not None
    assert await turns.re_offer(unanswered) is True

    start = await chat_store.next_prompt(replacement)
    assert start is not None
    assert start.prompt == "did you see this"
    assert await ledger.unanswered() is None, "the re-offered prompt is the one answering for it now"


async def test_an_unanswered_message_the_live_session_will_not_take_is_left_for_the_next_pass(
    turns: MatrixTurns,
    conversations: MatrixConversationStore,
    chat_store: SessionStore,
    ledger: IngressLedger,
    operator_id: UUID,
    thread: UUID,
) -> None:
    """Refusal is not the end of it. Nothing is recorded and nothing is said — the operator did not
    send this message again and has nothing to do about it — so the only correct answer is to ask
    once there is somewhere to ask."""
    doomed = await serving_session(chat_store, operator_id, thread)
    await serving_room(conversations, operator_id)
    await turns.offer([operator_message("did you see this", event_id="$1", at=1)])
    await chat_store.closed(doomed)

    unanswered = await ledger.unanswered()
    assert unanswered is not None
    assert await turns.re_offer(unanswered) is False

    assert await ledger.unanswered() == unanswered


async def test_an_unreadable_event_is_a_row_per_event_on_the_live_session(
    turns: MatrixTurns,
    conversations: MatrixConversationStore,
    chat_store: SessionStore,
    operator_id: UUID,
    thread: UUID,
) -> None:
    """What the notice says is a count and a set of types; what is kept is the events themselves
    (<../../../debug/channel_write_audit.md> row 12)."""
    session_id = await serving_session(chat_store, operator_id, thread)
    await serving_room(conversations, operator_id)

    rows = await turns.unreadable([_unmappable("m.image"), _unmappable("m.audio")])

    assert [(row.session_id, row.kind, row.body) for row in rows] == [
        (session_id, AuthoredEventKind.UNREADABLE_INPUT, {"media_type": "m.image"}),
        (session_id, AuthoredEventKind.UNREADABLE_INPUT, {"media_type": "m.audio"}),
    ]


async def test_an_unreadable_event_with_no_session_behind_the_room_records_nothing(
    turns: MatrixTurns, conversations: MatrixConversationStore, operator_id: UUID
) -> None:
    assert (await conversations.bind_room(MATRIX_ROOM, operator_id)).room_id == MATRIX_ROOM

    assert await turns.unreadable([_unmappable("m.image")]) == ()


async def test_a_batch_records_the_room_events_it_was_folded_from(
    turns: MatrixTurns,
    conversations: MatrixConversationStore,
    chat_store: SessionStore,
    migrated_sessions,
    operator_id: UUID,
    thread: UUID,
) -> None:
    """The prompt is what was said; which events said it rides on the prompt's own event, in the
    order they were folded. Nothing puts an event id in the text any more, so this is the
    only copy — and it names the room as well as the event, which is what a reader comparing
    origins needs once one bot serves more than one.
    """
    session_id = await serving_session(chat_store, operator_id, thread)
    await serving_room(conversations, operator_id)

    offered = await turns.offer(
        [operator_message("first", event_id="$a", at=1), operator_message("second", event_id="$b", at=2)]
    )

    assert isinstance(offered, PromptAccepted)
    async with migrated_sessions() as db:
        prompt = await db.get(SessionMessage, offered.message_id)
        assert prompt is not None
        assert prompt.content == "first\nsecond"
        asked = await db.scalar(
            select(SessionEvent).where(
                SessionEvent.session_id == session_id, SessionEvent.kind == AuthoredEventKind.PROMPT_ENQUEUED
            )
        )
    assert asked is not None
    assert PromptBody.model_validate(asked.body).origin == MatrixOrigin(address=MATRIX_ROOM, refs=("$a", "$b"))


if __name__ == "__main__":
    pytest_bazel.main()
