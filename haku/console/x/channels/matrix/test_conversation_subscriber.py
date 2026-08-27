"""Contracts of the room's own position, and of what it says and shows from it.

The durable half of <../../subscription.py>: this is the only place a position outlives the process
reading it, which is why the restart is asserted here rather than beside the abstraction. The span
fold's own cases live in <test_spans.py>; what is asserted here is the subscriber driving it — the
lines created, sealed, retired and swept off the same cursor the notices ride.
"""

from __future__ import annotations

import datetime
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.chat_models import LeaseExpiryReason, MatrixOrigin, PromptRejection, SpaOrigin
from haku.console.database_schema import ChannelCursor
from haku.console.x import conversation_log, session_events
from haku.console.x.channels.matrix.client import ConversationEventSource, ProjectedEvent, RoomEventKind
from haku.console.x.channels.matrix.conftest import MATRIX_ROOM
from haku.console.x.channels.matrix.conversation import MatrixConversationStore, RoomAttachment
from haku.console.x.channels.matrix.conversation_subscriber import (
    ABORTED_BY_OPERATOR,
    NOTHING_SAID,
    RELAYED_PROMPT,
    ConversationSubscriber,
    RoomCursor,
    project_notice,
)
from haku.console.x.channels.matrix.outbox import RoomOutbox
from haku.console.x.channels.matrix.room_copy import RoomCopy
from haku.console.x.channels.matrix.spans import PROVISIONING_STATUS, STATUS_EDIT_INTERVAL, Span
from haku.console.x.session_events import TurnAbortedBody, TurnAnsweredBody, TurnFailedBody
from haku.console.x.session_store import SessionStore
from haku.console.x.subscription import START, ConversationStream, StreamedEvent, StreamPosition


class Room:
    """What one room was told and shown, in the order it happened."""

    def __init__(self, expected_room: str = MATRIX_ROOM) -> None:
        self._expected_room = expected_room
        self.said: list[str] = []
        self.kinds: list[RoomEventKind] = []
        self.spans: list[tuple[str, str]] = []
        self.sealed: list[tuple[str, str]] = []
        self.retired: list[str] = []
        self.swept: list[frozenset[str]] = []
        self.typing: list[bool] = []
        self.projected: list[tuple[UUID, int]] = []
        self.fail_project = False

    async def record(self, body: str, kind: RoomEventKind) -> None:
        self.said.append(body)
        self.kinds.append(kind)

    async def project_notice(
        self, room_id: str, attachment_id: UUID, body: str, kind: RoomEventKind, conversation_id: UUID, event_seq: int
    ) -> None:
        assert room_id == self._expected_room
        assert attachment_id
        self.projected.append((conversation_id, event_seq))
        if self.fail_project:
            raise RuntimeError("homeserver refused the projected notice")
        await self.record(body, kind)

    async def show_span(self, room_id: str, attachment_id: UUID, span: Span, body: str) -> None:
        assert room_id == self._expected_room
        self.spans.append((span.subject, body))

    async def seal_span(self, room_id: str, attachment_id: UUID, span: Span, body: str) -> None:
        assert room_id == self._expected_room
        self.sealed.append((span.subject, body))

    async def retire_span(self, room_id: str, attachment_id: UUID, span: Span) -> None:
        self.retired.append(span.subject)

    async def retire_stale_spans(self, room_id: str, attachment_id: UUID, keep: frozenset[str]) -> None:
        self.swept.append(keep)

    async def set_typing(self, room_id: str, active: bool) -> None:
        self.typing.append(active)


class Clock:
    """A clock the test winds, so span floors need not be waited out.

    Seeded from the real clock because the fold compares it against rows' own `created_at`.
    """

    def __init__(self) -> None:
        self.now = datetime.datetime.now(datetime.UTC)

    def __call__(self) -> datetime.datetime:
        return self.now

    def tick(self, delta: datetime.timedelta) -> None:
        self.now += delta


@pytest.fixture
def room() -> Room:
    return Room()


@pytest.fixture
def stream(migrated_sessions: async_sessionmaker[AsyncSession]) -> ConversationStream:
    return ConversationStream(migrated_sessions)


@pytest.fixture
def outbox(migrated_sessions: async_sessionmaker[AsyncSession]) -> RoomOutbox:
    return RoomOutbox(migrated_sessions)


@pytest.fixture
def room_copy(migrated_sessions: async_sessionmaker[AsyncSession]) -> RoomCopy:
    return RoomCopy(migrated_sessions)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
async def binding(conversations: MatrixConversationStore, operator_id: UUID) -> RoomAttachment:
    """The room's live binding, which is what a subscriber is constructed for."""
    return await conversations.bind_room(MATRIX_ROOM, operator_id)


@pytest.fixture
def notices(
    migrated_sessions: async_sessionmaker[AsyncSession],
    stream: ConversationStream,
    conversation_wakes,
    room: Room,
    binding: RoomAttachment,
    outbox: RoomOutbox,
    room_copy: RoomCopy,
    clock: Clock,
) -> ConversationSubscriber:
    return ConversationSubscriber(
        migrated_sessions,
        stream,
        conversation_wakes,
        room.project_notice,
        room,
        binding,
        outbox,
        room_copy,
        clock=clock,
    )


@pytest.fixture
async def served(session_store: SessionStore, operator_id: UUID, binding: RoomAttachment) -> UUID:
    """A ready session serving the bound room, on the conversation the room is attached to."""
    view, token = await session_store.create(operator_id, conversation_id=binding.conversation_id)
    await session_store.authenticate_bridge(view.session_id, token)
    return view.session_id


async def abort_a_turn(session_store: SessionStore, operator_id: UUID, session_id: UUID) -> None:
    await session_store.enqueue_prompt(
        operator_id, session_id, "do the thing", MatrixOrigin(address=MATRIX_ROOM, refs=("$asked",))
    )
    turn = await session_store.next_prompt(session_id)
    assert turn is not None
    await session_store.end_turn(turn.turn_id, TurnAbortedBody())


async def stored_position(sessions: async_sessionmaker[AsyncSession]) -> StreamPosition | None:
    """Where the room has been brought up to, whichever attachment holds it — there is one."""
    async with sessions() as db:
        reached = await db.scalar(select(ChannelCursor.event_seq))
        return None if reached is None else StreamPosition(event_seq=reached)


async def test_a_room_that_has_never_read_takes_the_head_and_says_nothing(
    session_store, operator_id, served, notices, room, stream, migrated_sessions
) -> None:
    """A room the console has been servicing since before it kept a position already shows what was
    said in it, so a first pass must not replay the conversation into it."""
    await abort_a_turn(session_store, operator_id, served)

    assert await notices.reconcile_once() is False
    assert room.said == []
    assert await stored_position(migrated_sessions) == await stream.head(await session_store.conversation_of(served))


async def test_an_abort_recorded_after_the_room_started_reading_becomes_a_notice(
    session_store, operator_id, served, notices, room
) -> None:
    """An abort recorded while the room was reading becomes a notice."""
    await notices.reconcile_once()

    await abort_a_turn(session_store, operator_id, served)
    await notices.reconcile_once()

    assert room.said == [ABORTED_BY_OPERATOR]


async def test_a_failed_turn_tells_the_room_what_the_runtime_said(
    session_store, operator_id, served, notices, room
) -> None:
    """A failure is the one ending the room cannot infer: no answer arrives and nothing else is
    said, so the reason has to be carried in the runtime's own words or it reaches nobody here."""
    await notices.reconcile_once()
    await session_store.enqueue_prompt(
        operator_id, served, "do the thing", MatrixOrigin(address=MATRIX_ROOM, refs=("$asked",))
    )
    turn = await session_store.next_prompt(served)
    assert turn is not None
    await session_store.end_turn(turn.turn_id, TurnFailedBody(failure="upstream is at capacity"))

    await notices.reconcile_once()

    assert room.said == ["the turn failed — upstream is at capacity"]


async def test_an_answered_turn_without_a_message_becomes_a_silence_notice(
    session_store, operator_id, served, notices, room
) -> None:
    await notices.reconcile_once()
    await session_store.enqueue_prompt(
        operator_id, served, "do the thing", MatrixOrigin(address=MATRIX_ROOM, refs=("$asked",))
    )
    turn = await session_store.next_prompt(served)
    assert turn is not None
    await session_store.end_turn(turn.turn_id, TurnAnsweredBody())

    await notices.reconcile_once()

    assert room.said == [NOTHING_SAID]
    assert room.kinds == [RoomEventKind.NARRATION]


async def test_an_answered_turn_with_a_message_needs_no_silence_notice(
    session_store, operator_id, served, notices, room
) -> None:
    await notices.reconcile_once()
    await session_store.enqueue_prompt(
        operator_id, served, "do the thing", MatrixOrigin(address=MATRIX_ROOM, refs=("$asked",))
    )
    turn = await session_store.next_prompt(served)
    assert turn is not None
    await session_store.close_answer(served, turn.turn_id, final_text="done", frame_seq=1)
    await session_store.end_turn(turn.turn_id, TurnAnsweredBody(), last_frame_seq=1, projected_frame_seq=1)

    await notices.reconcile_once()

    assert room.said == []


async def test_turn_typing_is_derived_by_the_room_subscriber(session_store, operator_id, served, notices, room) -> None:
    await notices.reconcile_once()
    await session_store.enqueue_prompt(
        operator_id, served, "do the thing", MatrixOrigin(address=MATRIX_ROOM, refs=("$asked",))
    )
    turn = await session_store.next_prompt(served)
    assert turn is not None

    await notices.reconcile_once()
    assert room.typing[-1] is True

    await session_store.end_turn(turn.turn_id, TurnAnsweredBody())
    await notices.reconcile_once()
    assert room.typing[-1] is False


async def test_a_restarted_reader_rebuilds_active_typing_from_the_stream(
    session_store, operator_id, served, notices, room, migrated_sessions, stream, binding, conversation_wakes, outbox
) -> None:
    await notices.reconcile_once()
    await session_store.enqueue_prompt(
        operator_id, served, "do the thing", MatrixOrigin(address=MATRIX_ROOM, refs=("$asked",))
    )
    assert await session_store.next_prompt(served) is not None
    await notices.reconcile_once()

    successor_room = Room()
    successor = ConversationSubscriber(
        migrated_sessions,
        stream,
        conversation_wakes,
        successor_room.project_notice,
        successor_room,
        binding,
        outbox,
        RoomCopy(migrated_sessions),
    )
    await successor.reconcile_once()

    assert successor_room.typing[-1] is True


async def test_what_the_room_has_been_told_is_not_told_again(session_store, operator_id, served, notices, room) -> None:
    await notices.reconcile_once()
    await abort_a_turn(session_store, operator_id, served)

    await notices.reconcile_once()
    await notices.reconcile_once()

    assert room.said == [ABORTED_BY_OPERATOR]


async def test_a_restarted_reader_resumes_from_the_position_it_kept(
    session_store, operator_id, served, notices, room, migrated_sessions, stream, binding, conversation_wakes, outbox
) -> None:
    """The whole reason this position is durable: the room's copy outlives the process that wrote
    into it, so a replica that goes away mid-conversation must not re-say what its predecessor did
    — nor skip what was recorded while nobody was reading."""
    await notices.reconcile_once()
    await abort_a_turn(session_store, operator_id, served)
    await notices.reconcile_once()

    successor_room = Room()
    successor = ConversationSubscriber(
        migrated_sessions,
        stream,
        conversation_wakes,
        successor_room.project_notice,
        successor_room,
        binding,
        outbox,
        RoomCopy(migrated_sessions),
    )
    await successor.reconcile_once()
    assert successor_room.said == []

    await abort_a_turn(session_store, operator_id, served)
    await successor.reconcile_once()
    assert successor_room.said == [ABORTED_BY_OPERATOR]


async def author(
    sessions: async_sessionmaker[AsyncSession],
    session_store: SessionStore,
    session_id: UUID,
    body: session_events.AuthoredBody,
) -> None:
    """Write one of the console's own facts about *session_id*, as its own writer would."""
    conversation_id = await session_store.conversation_of(session_id)
    async with sessions() as db, db.begin():
        writer = await conversation_log.writer_for(
            db, conversation_id, session_id=session_id, turn_id=None, now=datetime.datetime.now(datetime.UTC)
        )
        writer.authored(body)


async def test_a_refused_prompt_is_said_from_its_row_rather_than_by_ingress(
    session_store, operator_id, served, notices, room, migrated_sessions
) -> None:
    """The row ingress wrote with the watermark is the notice: the sync loop records and this says
    it, so one refusal is not both recorded and pushed."""
    await notices.reconcile_once()
    await author(
        migrated_sessions,
        session_store,
        served,
        session_events.PromptRejectedBody(reason=PromptRejection.TURN_IN_FLIGHT, text="and this"),
    )

    await notices.reconcile_once()

    assert room.said == ["not delivered — Haku is still working on the previous message; send it again"]
    assert room.kinds == [RoomEventKind.REJECTED]


async def test_setup_narration_edits_the_session_line_rather_than_posting_a_notice(
    session_store, operator_id, served, notices, room, migrated_sessions, clock
) -> None:
    """The bootstrap narration is the loudest sender the room used to have — one notice per line.
    Folded into the session's span it is one line, edited."""
    await notices.reconcile_once()
    await author(migrated_sessions, session_store, served, session_events.SetupNarrationBody(text="cloning haku-state"))
    clock.tick(STATUS_EDIT_INTERVAL)

    await notices.reconcile_once()

    assert room.said == []
    assert [body for _, body in room.spans] == [PROVISIONING_STATUS, "cloning haku-state"]
    assert len({subject for subject, _ in room.spans}) == 1, "one line, edited in place"


async def test_something_haku_cannot_read_is_said_from_its_row(
    session_store, operator_id, served, notices, room, migrated_sessions
) -> None:
    await notices.reconcile_once()
    await author(migrated_sessions, session_store, served, session_events.UnreadableInputBody(media_type="m.image"))

    await notices.reconcile_once()

    assert "m.image" in room.said[0]
    assert room.kinds == [RoomEventKind.UNREADABLE]


async def test_a_session_ending_is_sealed_into_the_line_its_life_was_shown_on(
    session_store, operator_id, served, notices, room, migrated_sessions
) -> None:
    """Both are caused by a session and are conversation facts: what the operator needs is to know
    why the room went quiet. Adoption edits the session's one line, and the lease expiry seals it —
    a final edit that stays in scrollback — rather than each transition being its own notice."""
    await notices.reconcile_once()
    await author(
        migrated_sessions,
        session_store,
        served,
        session_events.SessionAdoptedBody(previous_holder="pod-a", holder="pod-b"),
    )
    await author(
        migrated_sessions,
        session_store,
        served,
        session_events.LeaseExpiredBody(reason=LeaseExpiryReason.HOLDER_GONE, last_holder="pod-b"),
    )

    await notices.reconcile_once()

    assert room.said == []
    [(subject, body)] = room.sealed
    assert (subject, body) == (room.spans[0][0], "the session ended — the console replica serving it went away")


async def test_a_lease_expiry_with_no_line_up_is_sealed_as_its_own_span(
    session_store, operator_id, served, notices, room, migrated_sessions
) -> None:
    """The degenerate seal: the session line was retired when its first turn started, so the ending
    posts as a one-event span — the sealed notice of the pre-span rendering, deduplicated the same
    way."""
    await notices.reconcile_once()
    await abort_a_turn(session_store, operator_id, served)
    await notices.reconcile_once()
    assert room.spans, "the session line was shown"
    assert room.retired, "the turn retired the session line"
    await author(
        migrated_sessions,
        session_store,
        served,
        session_events.LeaseExpiredBody(reason=LeaseExpiryReason.UNADOPTED, last_holder="pod-a"),
    )

    await notices.reconcile_once()

    [(subject, body)] = room.sealed
    assert body == "the session ended — its sandbox went away and nothing took it back over"
    assert subject not in {shown for shown, _ in room.spans}, "a span of its own, not the retired line"


async def test_the_first_turn_retires_the_pre_turn_session_line(
    session_store, operator_id, served, notices, room
) -> None:
    """A conversation that is moving is its own evidence of life, so the lifecycle line is spent
    the moment the first turn opens."""
    await notices.reconcile_once()
    assert [body for _, body in room.spans] == [PROVISIONING_STATUS]
    await session_store.enqueue_prompt(operator_id, served, "go", MatrixOrigin(address=MATRIX_ROOM, refs=("$go",)))
    assert await session_store.next_prompt(served) is not None

    await notices.reconcile_once()

    assert room.retired == [room.spans[0][0]]


async def test_stale_span_lines_are_swept_once_per_takeover(served, notices, room) -> None:
    """The sweep is the takeover repair — a redact lost with its replica, a line under a subject
    this release no longer writes — so it runs once per rebuilt fold, keeping what is open."""
    await notices.reconcile_once()
    await notices.reconcile_once()

    [kept] = room.swept
    assert kept == {room.spans[0][0]}


async def test_a_prompt_sent_from_another_surface_is_posted_into_the_room(
    session_store, operator_id, served, notices, room
) -> None:
    """A prompt is a conversation fact, so every attached surface shows it — including the one it
    did not arrive through."""
    await notices.reconcile_once()
    await session_store.enqueue_prompt(operator_id, served, "from the console", SpaOrigin())

    await notices.reconcile_once()

    assert room.said == [RELAYED_PROMPT + "from the console"]
    assert room.kinds == [RoomEventKind.NARRATION]


async def test_a_prompt_typed_into_this_room_is_not_posted_back_into_it(
    session_store, operator_id, served, notices, room
) -> None:
    """The prompt item's origin is what decides: the message is already in the timeline above, so
    posting it again would show the operator their own sentence twice."""
    await notices.reconcile_once()
    await session_store.enqueue_prompt(
        operator_id, served, "typed here", MatrixOrigin(address=MATRIX_ROOM, refs=("$here",))
    )

    await notices.reconcile_once()

    assert room.said == []


async def test_a_prompt_from_a_sibling_room_is_posted_because_the_address_differs(
    session_store, operator_id, served, notices, room
) -> None:
    """An equality test against the address, not a look inside one: a bare event id could not tell
    a sibling room's copy from this room's."""
    await notices.reconcile_once()
    await session_store.enqueue_prompt(
        operator_id, served, "next door", MatrixOrigin(address="!other:allegedly.works", refs=("$there",))
    )

    await notices.reconcile_once()

    assert room.said == [RELAYED_PROMPT + "next door"]


async def test_a_relayed_prompt_the_room_already_shows_is_not_posted_again(
    session_store, operator_id, served, notices, room, room_copy, binding
) -> None:
    """The relay rides the projection path now: its delivery is tied to the cursor and keyed by the
    prompt-completed event, so a crash replay finds the room's own copy instead of saying the
    operator's sentence twice."""
    await notices.reconcile_once()
    room.fail_project = True  # the send reached the homeserver; what died was everything after it
    await session_store.enqueue_prompt(operator_id, served, "from the console", SpaOrigin())
    with pytest.raises(RuntimeError, match="homeserver refused"):
        await notices.reconcile_once()
    [(conversation_id, seq)] = room.projected
    await room_copy.record(
        [
            ProjectedEvent(
                room_id=MATRIX_ROOM,
                event_id="$echoed",
                source=ConversationEventSource(
                    attachment_id=binding.attachment_id, conversation_id=conversation_id, event_seq=seq
                ),
                origin_server_ts=1,
                replaces_event_id=None,
            )
        ],
        [],
    )
    room.fail_project = False

    await notices.reconcile_once()

    assert (room.said, len(room.projected)) == ([], 1)


async def test_a_silence_notice_that_failed_to_send_is_replayed_with_the_same_source(
    session_store, operator_id, served, notices, room, migrated_sessions
) -> None:
    """The cursor follows the accepted effect for silence too: a failed send leaves the turn's
    answered event owed, and the retry names the same durable source."""
    await notices.reconcile_once()
    before = await stored_position(migrated_sessions)
    room.fail_project = True
    await session_store.enqueue_prompt(
        operator_id, served, "do the thing", MatrixOrigin(address=MATRIX_ROOM, refs=("$asked",))
    )
    turn = await session_store.next_prompt(served)
    assert turn is not None
    await session_store.end_turn(turn.turn_id, TurnAnsweredBody())

    with pytest.raises(RuntimeError, match="homeserver refused"):
        await notices.reconcile_once()
    assert await stored_position(migrated_sessions) == before

    room.fail_project = False
    await notices.reconcile_once()

    assert room.projected[-1] == room.projected[-2]
    assert room.said == [NOTHING_SAID]


async def test_an_unread_room_has_no_position_at_all(migrated_sessions) -> None:
    """Absent is "never read", not "at the start" — the distinction the seeding above turns on."""
    assert await RoomCursor(migrated_sessions, uuid4()).position() is None


async def test_a_sibling_rooms_subscriber_reads_only_its_own_conversation(
    session_store,
    operator_id,
    conversations,
    served,
    notices,
    room,
    migrated_sessions,
    stream,
    conversation_wakes,
    outbox,
    room_copy,
) -> None:
    """Attachment-scoped reconciliation: each room's subscriber folds its own conversation off its
    own cursor, so a fact recorded on one thread reaches only that thread's room."""
    sibling_binding = await conversations.bind_room("!second:allegedly.works", operator_id)
    sibling_room = Room("!second:allegedly.works")
    sibling = ConversationSubscriber(
        migrated_sessions,
        stream,
        conversation_wakes,
        sibling_room.project_notice,
        sibling_room,
        sibling_binding,
        outbox,
        room_copy,
    )
    await notices.reconcile_once()
    await sibling.reconcile_once()

    await abort_a_turn(session_store, operator_id, served)
    await notices.reconcile_once()
    await sibling.reconcile_once()

    assert room.said == [ABORTED_BY_OPERATOR]
    assert sibling_room.said == []


async def test_keeping_a_position_twice_moves_it_rather_than_failing(migrated_sessions, binding) -> None:
    """Whichever replica leads the sync loop writes this row, and leadership changes hands."""
    cursor = RoomCursor(migrated_sessions, binding.attachment_id)

    await cursor.keep(START)
    await cursor.keep(StreamPosition(event_seq=17))

    assert await cursor.position() == StreamPosition(event_seq=17)


async def test_keeping_an_older_position_cannot_rewind_the_room(migrated_sessions, binding) -> None:
    cursor = RoomCursor(migrated_sessions, binding.attachment_id)

    await cursor.keep(StreamPosition(event_seq=17))
    await cursor.keep(StreamPosition(event_seq=3))

    assert await cursor.position() == StreamPosition(event_seq=17)


async def test_a_replayed_projection_already_in_the_room_is_not_sent_again(
    session_store,
    operator_id,
    served,
    notices,
    room,
    room_copy,
    binding,
    migrated_sessions,
    stream,
    conversation_wakes,
    outbox,
) -> None:
    """Restart after Synapse's transaction cache would have expired.

    The send reached the homeserver and the crash ate everything after it, so the cursor still
    names the event. The replay finds the room already showing the source — via the tag its own
    echo carried — and sends nothing at all, which is what makes the cache's 30-to-60-minute
    lifetime not load-bearing: a send that never happens needs no deduplication.
    """
    await notices.reconcile_once()
    room.fail_project = True  # the send reached the homeserver; what died was everything after it
    await abort_a_turn(session_store, operator_id, served)
    with pytest.raises(RuntimeError, match="homeserver refused"):
        await notices.reconcile_once()
    [(conversation_id, seq)] = room.projected
    # The send's own echo, as the sync loop records it observing the room.
    await room_copy.record(
        [
            ProjectedEvent(
                room_id=MATRIX_ROOM,
                event_id="$echoed",
                source=ConversationEventSource(
                    attachment_id=binding.attachment_id, conversation_id=conversation_id, event_seq=seq
                ),
                origin_server_ts=1,
                replaces_event_id=None,
            )
        ],
        [],
    )

    successor_room = Room()
    successor = ConversationSubscriber(
        migrated_sessions,
        stream,
        conversation_wakes,
        successor_room.project_notice,
        successor_room,
        binding,
        outbox,
        room_copy,
    )
    await successor.reconcile_once()

    assert (successor_room.projected, successor_room.said) == ([], [])
    assert await stored_position(migrated_sessions) == await stream.head(conversation_id), (
        "the suppressed event is finished with, not deferred"
    )


async def test_a_failed_projection_is_replayed_with_the_same_source_identity(
    session_store, operator_id, served, notices, room, migrated_sessions
) -> None:
    """The cursor follows the accepted effect. A failed send leaves the row owed, and the retry
    names the same durable source for Matrix transaction deduplication."""
    await notices.reconcile_once()
    before = await stored_position(migrated_sessions)
    room.fail_project = True
    await abort_a_turn(session_store, operator_id, served)

    with pytest.raises(RuntimeError, match="homeserver refused"):
        await notices.reconcile_once()
    assert await stored_position(migrated_sessions) == before

    room.fail_project = False
    await notices.reconcile_once()

    assert len(room.projected) == 2
    assert room.projected[0] == room.projected[1]
    assert room.said == [ABORTED_BY_OPERATOR]


@pytest.mark.parametrize(
    ("body", "expected", "kind"),
    [
        (
            session_events.PromptRejectedBody(reason=PromptRejection.TURN_IN_FLIGHT, text="wait"),
            "not delivered — Haku is still working on the previous message; send it again",
            RoomEventKind.REJECTED,
        ),
        (
            session_events.UnreadableInputBody(media_type="m.image"),
            "received a message Haku cannot read (m.image) — it reads text only; "
            "describe it in words and it will reach the session",
            RoomEventKind.UNREADABLE,
        ),
        (session_events.TurnAbortedBody(), ABORTED_BY_OPERATOR, RoomEventKind.LIFECYCLE),
        (
            session_events.TurnFailedBody(failure="the model provider is at capacity"),
            "the turn failed — the model provider is at capacity",
            RoomEventKind.LIFECYCLE,
        ),
    ],
)
def test_sealed_notices_are_pure_projections_of_their_source_event(body, expected, kind) -> None:
    conversation_id = uuid4()
    event = StreamedEvent(
        position=StreamPosition(event_seq=23),
        session_id=uuid4(),
        turn_id=uuid4(),
        item_id=None,
        created_at=datetime.datetime(2026, 8, 21, tzinfo=datetime.UTC),
        body=body,
    )

    projected = project_notice(event, conversation_id=conversation_id, room_id=MATRIX_ROOM)

    assert projected is not None
    assert (projected.body, projected.kind) == (expected, kind)
    assert (projected.conversation_id, projected.source_event_seq) == (conversation_id, 23)


if __name__ == "__main__":
    pytest_bazel.main()
