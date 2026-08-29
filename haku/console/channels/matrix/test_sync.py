"""One sync pass: what gets joined, what reaches the session, and what is recorded about it.

The watermark moves on every pass, so what these assert is the other half: a batch the session
would not take is rejected, said so in the room, and written down in the transaction that
acknowledges it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from sqlalchemy import select

from haku.console.channels.matrix.client import (
    AuthError,
    ConversationEventSource,
    Error,
    EventTag,
    InboundMessage,
    Invite,
    ProjectedEvent,
    RoomEventKind,
    SyncResult,
    UnmappableEvent,
)
from haku.console.channels.matrix.conftest import (
    MATRIX_CONFIG,
    MATRIX_OPERATOR,
    MATRIX_ROOM,
    MATRIX_TEST_HARNESS_KIND,
    MATRIX_USER,
)
from haku.console.channels.matrix.conversation import (
    Admission,
    ConversationFacts,
    ConversationStore,
    PromptAccepted,
    PromptRejected,
    RoomAttachment,
)
from haku.console.channels.matrix.ingress_ledger import IngressLedger
from haku.console.channels.matrix.outbox import PendingReply
from haku.console.channels.matrix.pacer import RoomPacers
from haku.console.channels.matrix.revisions import RevisionLog
from haku.console.channels.matrix.room_copy import RoomCopy
from haku.console.channels.matrix.spans import Span, SpanKind
from haku.console.channels.matrix.sync import SyncService, SyncStore
from haku.console.conversation.conversation_event import (
    AuthoredEventKind,
    PromptRejected as PromptRejectedEvent,  # the record body; `conversation.PromptRejected` is the admission answer
    PromptRejection,
    StoredEventKind,
    UnreadableInput,
)
from haku.console.conversation.prompt_origin import MatrixOrigin
from haku.console.database_schema import ConversationEventRow
from haku.console.harnesses.kind import HarnessKind
from haku.console.session.store import BridgeAuthentication, Store
from haku.console.session.subscription import ConversationStream


@dataclass
class _FakeMatrix:
    """Records what the service asked the homeserver to do."""

    result: SyncResult
    joined: list[str] = field(default_factory=list)
    sent: list[tuple[str, str]] = field(default_factory=list)
    notices: list[tuple[str, str]] = field(default_factory=list)
    edits: list[tuple[str, str]] = field(default_factory=list)
    redacted: list[str] = field(default_factory=list)
    tags: list[EventTag] = field(default_factory=list)
    transactions: list[str] = field(default_factory=list)
    since: str | None = None
    token_valid: bool = True
    logins: int = 0

    async def whoami(self, token: str) -> bool:
        return self.token_valid

    async def login(self, password: str) -> str:
        self.logins += 1
        return "fresh-token"

    async def sync(self, token: str, since: str | None) -> SyncResult:
        self.since = since
        return self.result

    async def join(self, token: str, room_id: str) -> None:
        self.joined.append(room_id)

    async def send_text(self, token: str, room_id: str, body: str, txn_id: str, tag: EventTag) -> str:
        self.sent.append((room_id, body))
        self.tags.append(tag)
        self.transactions.append(txn_id)
        return f"$sent-{len(self.sent)}"

    async def send_notice(self, token: str, room_id: str, body: str, txn_id: str, tag: EventTag) -> str:
        self.notices.append((room_id, body))
        self.tags.append(tag)
        self.transactions.append(txn_id)
        return f"$notice-{len(self.notices)}"

    async def edit_notice(self, token: str, room_id: str, event_id: str, body: str, txn_id: str, tag: EventTag) -> None:
        self.edits.append((event_id, body))
        self.tags.append(tag)
        self.transactions.append(txn_id)

    async def redact(self, token: str, room_id: str, event_id: str, reason: str) -> None:
        self.redacted.append(event_id)


@dataclass
class _FakeTurns:
    """Ingress as the loop sees it: it accepts or rejects a batch, and says what to record.

    What it hands back are bodies and the conversation they belong to — read off the binding the
    loop dispatched the batch with, the way the real ingress does — because an authored row's
    position is allocated under that conversation's lock, so what the loop does with them is append
    them where it moves the watermark.
    """

    session_id: UUID | None = None
    accepts: bool = True
    reason: PromptRejection = PromptRejection.TURN_IN_FLIGHT
    offered: list[list[str]] = field(default_factory=list)
    offered_to: list[tuple[str, UUID]] = field(default_factory=list)

    async def offer(self, binding: RoomAttachment, messages: Sequence[InboundMessage]) -> Admission:
        self.offered.append([message.body for message in messages])
        self.offered_to.append((binding.room_id, binding.conversation_id))
        if self.accepts:
            return PromptAccepted(prompt_id=uuid4())
        return PromptRejected(
            reason=self.reason,
            facts=self._facts(binding, PromptRejectedEvent(reason=self.reason, text="\n".join(self.offered[-1]))),
        )

    async def unreadable(self, binding: RoomAttachment, events: Sequence[UnmappableEvent]) -> ConversationFacts:
        return self._facts(binding, *(UnreadableInput(media_type=event.msgtype) for event in events))

    def _facts(self, binding: RoomAttachment, *bodies: PromptRejectedEvent | UnreadableInput) -> ConversationFacts:
        return ConversationFacts(
            conversation_id=binding.conversation_id, session_id=self.session_id, bodies=tuple(bodies)
        )


@pytest.fixture
def sync_store(migrated_sessions) -> SyncStore:
    return SyncStore(migrated_sessions)


@pytest.fixture
def turns() -> _FakeTurns:
    """Ingress as the loop drives it: which conversation a fact lands on is the binding's."""
    return _FakeTurns()


@pytest.fixture
def matrix() -> _FakeMatrix:
    """The homeserver. Tests set `matrix.result` to the sync response under test."""
    return _FakeMatrix(SyncResult("s2", (), ()))


def _replica(sync_store, conversations, identities, turns, matrix, migrated_sessions, ledger) -> SyncService:
    """One console replica's sync service, unthrottled.

    The real budget is `test_pacer`'s subject; at the room's true rate each of these would wait
    five seconds per send to assert something that is not about waiting.
    """
    service = SyncService(
        MATRIX_CONFIG,
        engine=cast(Any, None),  # only `run()` takes the advisory lock; these drive one pass
        store=sync_store,
        conversations=conversations,
        identities=identities,
        turns=cast(Any, turns),
        # Answers are outbox rows, drained by the reconcilers `run()` sweeps up; these tests drive
        # one sync pass and assert the narration, which never touches the table (`test_outbox` does).
        outbox=cast(Any, None),
        revisions=RevisionLog(migrated_sessions),
        ledger=ledger,
        room_copy=RoomCopy(migrated_sessions),
        # The sync leader starts the wake wire with its reconcilers, inside `run()` (see `outbox`).
        outbox_wakes=cast(Any, None),
        sessions=migrated_sessions,
        stream=ConversationStream(migrated_sessions),
        # Consumed only by the reconcilers' subscribers, which these single-pass tests never start.
        notifications=cast(Any, None),
        new_conversation_harness_kind=MATRIX_TEST_HARNESS_KIND,
    )
    service._client = cast(Any, matrix)
    service.pacers = RoomPacers(sends_per_second=1e6, burst=1_000)
    return service


@pytest.fixture
async def service(sync_store, conversations, migrated_identity_store, turns, matrix, migrated_sessions, ledger):
    """The service with its outbound queues running, because every send goes through them."""
    service = _replica(sync_store, conversations, migrated_identity_store, turns, matrix, migrated_sessions, ledger)
    async with service.pacers.run():
        yield service


async def settled(service: SyncService) -> None:
    """Wait for what the service queued to actually reach the homeserver."""
    await service.pacers.flush()


@pytest.fixture
async def bound_room(conversations: ConversationStore, operator_id: UUID) -> str:
    """Most tests start from a room already bound; the adoption/invite ones do not use this.

    Binding is what attaches it, so this is also the row the status line's own event id hangs off.
    """
    return (await conversations.bind_room(MATRIX_ROOM, operator_id, harness_kind=MATRIX_TEST_HARNESS_KIND)).room_id


async def watermark(store: SyncStore) -> str | None:
    return await store.watermark(MATRIX_USER)


async def recorded(sessions) -> list[tuple[StoredEventKind, dict[str, Any]]]:
    """The ingress facts a pass wrote, excluding lifecycle facts from fixture setup."""
    async with sessions() as db:
        rows = (
            await db.scalars(
                select(ConversationEventRow)
                .where(
                    ConversationEventRow.kind.in_(
                        (AuthoredEventKind.PROMPT_REJECTED, AuthoredEventKind.UNREADABLE_INPUT)
                    )
                )
                .order_by(ConversationEventRow.event_seq)
            )
        ).all()
    return [(row.kind, row.body) for row in rows]


def _queued(body: str, *, item_id: UUID | None = None) -> PendingReply:
    """A row as the drain would hand it over, without going near the table it came out of."""
    subject = item_id or uuid4()
    return PendingReply(
        outbox_id=uuid4(), attachment_id=uuid4(), room_id=MATRIX_ROOM, subject=subject.hex, body=body, attempts=1
    )


def _message(body: str, sender: str = MATRIX_OPERATOR, event_id: str = "$evt") -> InboundMessage:
    return InboundMessage(room_id=MATRIX_ROOM, event_id=event_id, sender=sender, body=body, origin_server_ts=1)


def _unreadable(msgtype: str = "m.image", event_id: str = "$img", room_id: str = MATRIX_ROOM) -> UnmappableEvent:
    return UnmappableEvent(room_id=room_id, event_id=event_id, sender=MATRIX_OPERATOR, msgtype=msgtype)


async def test_hands_an_operator_message_to_the_session(service, matrix, turns, bound_room):
    matrix.result = SyncResult("s2", (_message("hello"),), ())

    await service.sync_once("tok")

    assert turns.offered == [["hello"]]


async def test_a_batch_is_offered_as_one_prompt(service, matrix, turns, bound_room):
    """Several messages in one sync response are one turn, not several."""
    matrix.result = SyncResult("s2", (_message("first", event_id="$a"), _message("second", event_id="$b")), ())

    await service.sync_once("tok")

    assert turns.offered == [["first", "second"]]


async def carried_prompt(
    session_store: Store, operator_id: UUID, ledger: IngressLedger, event_id: str, body: str
) -> UUID:
    """A prompt in the record carrying *event_id*, as an accepted batch leaves one behind."""
    view, token = await session_store.create(operator_id, harness_kind=HarnessKind.CLAUDE_CODE)
    assert await session_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await session_store.submit_prompt(
        operator_id,
        await session_store.conversation_of(view.session_id),
        f"[{event_id}] {body}",
        MatrixOrigin(address=MATRIX_ROOM, refs=(event_id,)),
        ledger.carrying((event_id,)),
    )
    return view.session_id


async def test_a_re_delivered_message_is_dropped_from_the_batch(
    service, matrix, turns, sync_store, session_store, operator_id, ledger, bound_room
):
    """The crash this closes: the prompt committed, the watermark did not, and `/sync` hands the
    same event back. Offering it again would ask twice — and be refused, since the first copy is
    still queued, so the room would report a message as undelivered that the session is about to
    answer."""
    await carried_prompt(session_store, operator_id, ledger, "$a", "hello")
    matrix.result = SyncResult("s2", (_message("hello", event_id="$a"),), ())

    await service.sync_once("tok")
    await settled(service)

    assert turns.offered == []
    assert await watermark(sync_store) == "s2"
    assert matrix.notices == []


async def test_only_the_re_delivered_half_of_a_batch_is_dropped(
    service, matrix, turns, session_store, operator_id, ledger, bound_room
):
    """A restart can land the crashed batch and what was said since in one response."""
    await carried_prompt(session_store, operator_id, ledger, "$a", "hello")
    matrix.result = SyncResult("s2", (_message("hello", event_id="$a"), _message("and this", event_id="$b")), ())

    await service.sync_once("tok")

    assert turns.offered == [["and this"]]


async def test_a_rejected_batch_is_acknowledged_and_recorded_in_one_go(
    service, matrix, turns, sync_store, migrated_sessions, bound_room
):
    """A prompt the session will not take is rejected, not held.

    The watermark covers the batch, so the homeserver will not offer it again, and the only
    surviving copy of what was said is the row written beside that watermark. The room hears about
    it from that row (`conversation_subscriber.project_notice`), so this pass says nothing itself.
    """
    matrix.result = SyncResult("s2", (_message("hello"),), ())
    turns.accepts = False

    await service.sync_once("tok")
    await settled(service)

    assert await watermark(sync_store) == "s2"
    assert await recorded(migrated_sessions) == [
        (AuthoredEventKind.PROMPT_REJECTED, {"reason": PromptRejection.TURN_IN_FLIGHT, "text": "hello"})
    ]
    assert matrix.notices == []


async def test_a_recording_that_cannot_be_written_takes_the_watermark_with_it(sync_store, migrated_sessions):
    """The transaction, tested by breaking it: a fact naming a conversation that does not exist has
    nowhere to take a position from, and what must not survive that is the acknowledgement.

    Advancing separately would leave the homeserver told the message was handled with nothing
    written about it and nothing said."""
    orphan = ConversationFacts(
        conversation_id=uuid4(), session_id=None, bodies=(UnreadableInput(media_type="m.image"),)
    )

    with pytest.raises(KeyError):
        await sync_store.advance(MATRIX_USER, "s2", (orphan,))

    assert await watermark(sync_store) is None
    assert await recorded(migrated_sessions) == []


async def test_every_rejected_batch_gets_a_row_of_its_own(service, matrix, turns, migrated_sessions, bound_room):
    """Once per rejection, not once per turn: nothing is re-offered, so each of these rows is about
    a different message the operator has to send again."""
    matrix.result = SyncResult("s2", (_message("hello"),), ())
    turns.accepts = False
    await service.sync_once("tok")

    matrix.result = SyncResult("s3", (_message("and this", event_id="$b"),), ())
    await service.sync_once("tok")
    await settled(service)

    assert [body["text"] for _, body in await recorded(migrated_sessions)] == ["hello", "and this"]
    assert turns.offered == [["hello"], ["and this"]]


async def test_a_pass_with_nothing_to_reject_says_nothing(service, matrix, turns, bound_room):
    """A quiet pass and an accepted one are both silent — the room hears about its own messages,
    not about the loop running."""
    matrix.result = SyncResult("s2", (_message("hello"),), ())
    await service.sync_once("tok")

    matrix.result = SyncResult("s3", (), ())
    await service.sync_once("tok")
    await settled(service)

    assert matrix.notices == []


async def test_a_message_arriving_mid_turn_is_recorded_as_undelivered(
    service, matrix, turns, migrated_sessions, bound_room
):
    """A batch arriving while a turn runs is rejected rather than held, which costs the operator a
    re-send and nothing else."""
    matrix.result = SyncResult("s2", (_message("hello"),), ())
    await service.sync_once("tok")

    turns.accepts = False
    matrix.result = SyncResult("s3", (_message("and this", event_id="$b"),), ())
    await service.sync_once("tok")
    await settled(service)

    assert await recorded(migrated_sessions) == [
        (AuthoredEventKind.PROMPT_REJECTED, {"reason": PromptRejection.TURN_IN_FLIGHT, "text": "and this"})
    ]


async def test_a_rejection_with_no_session_behind_the_room_is_still_recorded(
    service, matrix, turns, sync_store, migrated_sessions, bound_room
):
    """The rejection that used to have nowhere to write itself.

    A row named a session, and a room whose sandbox has not been provisioned has none — so this was
    said into the room by ingress and kept nowhere. What a refusal is about is the conversation, and
    that exists from the moment the room is bound, so it is a row like every other refusal's, with
    no session named because there was none. `ConversationSubscriber` says it from that row, which is why this
    pass says nothing itself.
    """
    matrix.result = SyncResult("s2", (_message("hello"),), ())
    turns.accepts = False
    turns.reason = PromptRejection.NO_SESSION

    await service.sync_once("tok")
    await settled(service)

    assert await watermark(sync_store) == "s2"
    assert await recorded(migrated_sessions) == [
        (AuthoredEventKind.PROMPT_REJECTED, {"reason": PromptRejection.NO_SESSION, "text": "hello"})
    ]
    assert matrix.notices == []


async def test_an_unreadable_event_is_recorded_and_the_batch_moves_on(
    service, matrix, turns, sync_store, migrated_sessions, bound_room
):
    """The half a refusal cannot serve: nothing about a sent `m.image` will ever change, so holding
    the batch for it would wedge ingress on one screenshot forever. The fact is written down beside
    the watermark, and what the room shows is a rendering of that row."""
    matrix.result = SyncResult("s2", (), (), (_unreadable(),))

    await service.sync_once("tok")
    await settled(service)

    assert await watermark(sync_store) == "s2"
    assert turns.offered == [], "there is no prose in this batch to hand over"
    assert await recorded(migrated_sessions) == [(AuthoredEventKind.UNREADABLE_INPUT, {"media_type": "m.image"})]
    assert matrix.notices == []


async def test_each_unreadable_event_is_a_row_of_its_own(service, matrix, turns, migrated_sessions, bound_room):
    """One row each rather than one row for the batch: what arrived is two facts, and a channel
    that wants to summarise them can, from two rows."""
    matrix.result = SyncResult("s2", (), (), (_unreadable(), _unreadable(msgtype="m.audio", event_id="$memo")))

    await service.sync_once("tok")
    await settled(service)

    assert [body["media_type"] for _, body in await recorded(migrated_sessions)] == ["m.image", "m.audio"]


async def test_the_text_of_a_mixed_batch_is_serviced_and_the_rest_recorded(
    service, matrix, turns, migrated_sessions, bound_room
):
    """A "look at this" alongside a screenshot: the sentence still starts a turn."""
    matrix.result = SyncResult("s2", (_message("look at this"),), (), (_unreadable(),))

    await service.sync_once("tok")
    await settled(service)

    assert turns.offered == [["look at this"]]
    assert await recorded(migrated_sessions) == [(AuthoredEventKind.UNREADABLE_INPUT, {"media_type": "m.image"})]


async def test_an_unreadable_event_from_an_unserviced_room_reaches_nothing(
    service, matrix, migrated_sessions, bound_room
):
    """Only rooms holding a conversation are serviced, and an unreadable event adopts none — there
    is no prose in it to answer — so a stray room's screenshot is neither recorded nor said."""
    matrix.result = SyncResult("s2", (), (), (_unreadable(room_id="!stray:allegedly.works"),))

    await service.sync_once("tok")
    await settled(service)

    assert (matrix.notices, await recorded(migrated_sessions)) == ([], [])


async def test_joins_an_invite_from_the_operator(service, matrix):
    matrix.result = SyncResult("s2", (), (Invite(room_id=MATRIX_ROOM, inviter=MATRIX_OPERATOR),))

    await service.sync_once("tok")

    assert matrix.joined == [MATRIX_ROOM]


async def test_a_second_invite_joins_and_binds_a_conversation_of_its_own(service, matrix, conversations, bound_room):
    """One bot serves many rooms: a second invite is joined and bound beside the first, each room
    its own conversation."""
    other = "!other:allegedly.works"
    matrix.result = SyncResult("s2", (), (Invite(room_id=other, inviter=MATRIX_OPERATOR),))

    await service.sync_once("tok")

    await settled(service)
    assert matrix.joined == [other]
    [(room_id, body)] = matrix.notices
    assert (room_id, "joined" in body) == (other, True)
    bindings = await conversations.live_attachments()
    assert [binding.room_id for binding in bindings] == [MATRIX_ROOM, other]
    assert bindings[0].conversation_id != bindings[1].conversation_id


async def test_leaves_an_invite_from_anybody_else_pending(service, matrix):
    """Only the operator's invites are joined; others are surfaced, not acted on."""
    stranger = Invite(room_id="!other:allegedly.works", inviter="@stranger:allegedly.works")
    matrix.result = SyncResult("s2", (), (stranger,))

    await service.sync_once("tok")

    assert matrix.joined == []


async def test_adopts_an_unbound_room_from_operator_traffic(service, matrix, turns):
    """Being in the room already required an operator invite, so a binding can be recovered."""
    stray = InboundMessage("!already-joined:allegedly.works", "$e", MATRIX_OPERATOR, "hi", 1)
    matrix.result = SyncResult("s2", (stray,), ())

    await service.sync_once("tok")

    await settled(service)
    assert turns.offered == [["hi"]], "the adopting batch is serviced, not dropped"
    [(room_id, body)] = matrix.notices
    assert room_id == "!already-joined:allegedly.works"
    assert "adopted" in body


async def test_does_not_adopt_from_a_sender_who_is_not_the_operator(service, matrix, turns):
    """Adoption inherits the invite rule: only the operator can cause Haku to bind a room."""
    stray = InboundMessage("!elsewhere:allegedly.works", "$e", "@stranger:allegedly.works", "hi", 1)
    matrix.result = SyncResult("s2", (stray,), ())

    await service.sync_once("tok")

    assert turns.offered == []
    assert matrix.notices == []


async def test_operator_traffic_in_an_unbound_room_is_adopted_beside_the_existing_binding(
    service, matrix, turns, bound_room
):
    """Adoption is per room, not only for a console with nothing bound: a joined room the operator
    is speaking in gets its own binding beside the live one, and its batch is serviced."""
    stray = InboundMessage("!stray:allegedly.works", "$e", MATRIX_OPERATOR, "hi", 1)
    matrix.result = SyncResult("s2", (stray,), ())

    await service.sync_once("tok")

    await settled(service)
    assert [room for room, _ in turns.offered_to] == ["!stray:allegedly.works"]
    assert turns.offered == [["hi"]]


async def test_each_rooms_messages_are_offered_to_its_own_conversation(
    service, matrix, turns, conversations, operator_id, bound_room
):
    """The dispatch itself: one batch carrying two rooms' messages becomes one offer per room, each
    against the conversation its attachment names, in the order the rooms appear in the batch."""
    other = (
        await conversations.bind_room("!other:allegedly.works", operator_id, harness_kind=MATRIX_TEST_HARNESS_KIND)
    ).room_id
    matrix.result = SyncResult(
        "s2",
        (
            _message("first here", event_id="$a"),
            InboundMessage(other, "$b", MATRIX_OPERATOR, "and there", 2),
            _message("more here", event_id="$c"),
        ),
        (),
    )

    await service.sync_once("tok")

    bindings = {binding.room_id: binding.conversation_id for binding in await conversations.live_attachments()}
    assert turns.offered == [["first here", "more here"], ["and there"]]
    assert turns.offered_to == [(MATRIX_ROOM, bindings[MATRIX_ROOM]), (other, bindings[other])]


async def test_posting_a_queued_reply_says_it_as_text(service, matrix, sync_store, bound_room):
    matrix.result = SyncResult("s2", (), ())
    await sync_store.save_token(MATRIX_USER, "cached")

    await service.post_reply(_queued("the answer"))

    assert matrix.sent == [(MATRIX_ROOM, "the answer")]


async def test_a_projected_notice_uses_its_durable_source_as_the_transaction(service, matrix, bound_room) -> None:
    attachment_id = UUID("11111111-2222-4333-8444-555555555555")
    conversation_id = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")

    await service.project_notice(
        bound_room, attachment_id, "the session ended", RoomEventKind.LIFECYCLE, conversation_id, 17
    )
    await service.project_notice(
        bound_room, attachment_id, "the session ended", RoomEventKind.LIFECYCLE, conversation_id, 17
    )

    assert matrix.notices == [(bound_room, "the session ended"), (bound_room, "the session ended")]
    assert matrix.transactions[0] == matrix.transactions[1]
    assert (
        matrix.tags
        == [
            EventTag(
                kind=RoomEventKind.LIFECYCLE,
                source=ConversationEventSource(
                    attachment_id=attachment_id, conversation_id=conversation_id, event_seq=17
                ),
            )
        ]
        * 2
    )


async def test_a_quiet_batch_advances_the_watermark(service, matrix, sync_store, bound_room):
    matrix.result = SyncResult("s2", (), ())

    await service.sync_once("tok")

    assert await watermark(sync_store) == "s2"


async def test_a_batch_handed_over_is_acknowledged_at_once(service, matrix, turns, sync_store, bound_room):
    """Acceptance is the acknowledgement, and what it costs is that a prompt whose session ends
    before claiming it is not offered again — what answers it is the replacement session being
    handed the transcript it is already in (`ConversationHistory.recent`)."""
    matrix.result = SyncResult("s2", (_message("hi"),), ())

    await service.sync_once("tok")

    assert (await watermark(sync_store), turns.offered) == ("s2", [["hi"]])


async def test_the_next_poll_starts_from_the_watermark(service, matrix, turns, sync_store, bound_room):
    """One position, so the cursor and the promise are the same token. `/sync` long-polls only for
    data the caller has not been sent, so a loop reading from behind its own acknowledgement would
    be handed the same batch every pass."""
    matrix.result = SyncResult("s2", (_message("hi"),), ())
    await service.sync_once("tok")

    matrix.result = SyncResult("s3", (), ())
    await service.sync_once("tok")

    assert matrix.since == "s2"
    assert turns.offered == [["hi"]], "and the batch it covers is not offered a second time"


async def test_resumes_from_the_stored_watermark(service, matrix, sync_store, bound_room):
    matrix.result = SyncResult("s5", (), ())
    await sync_store.advance(MATRIX_USER, "s4")

    await service.sync_once("tok")

    assert matrix.since == "s4"


async def test_the_token_and_the_watermark_can_be_first_written_at_once(sync_store):
    """A queued send logging in and the sync pass advancing the watermark are two writers with
    nothing to say to each other, and each starts with no row to update. They own a table each, so
    the first write of one is not the other's primary-key collision."""
    await asyncio.gather(sync_store.save_token(MATRIX_USER, "cached"), sync_store.advance(MATRIX_USER, "s2"))

    assert (await sync_store.cached_token(MATRIX_USER), await watermark(sync_store)) == ("cached", "s2")


async def test_reuses_a_valid_cached_token(service, matrix, sync_store, bound_room):
    """Synapse rate-limits /login, so a working token must not be re-minted."""
    matrix.result = SyncResult("s2", (), ())
    await sync_store.save_token(MATRIX_USER, "cached")

    assert await service._token() == "cached"
    assert matrix.logins == 0


async def test_logs_in_again_when_the_cached_token_is_rejected(service, matrix, sync_store, bound_room):
    matrix.result = SyncResult("s2", (), ())
    await sync_store.save_token(MATRIX_USER, "stale")
    matrix.token_valid = False

    assert await service._token() == "fresh-token"
    assert (matrix.logins, await sync_store.cached_token(MATRIX_USER)) == (1, "fresh-token")


async def test_auth_error_surfaces_so_the_loop_can_re_login(service, matrix, bound_room):
    """The loop distinguishes a rejected token from a transport failure."""

    async def _reject(token: str, since: str | None) -> SyncResult:
        raise AuthError("M_UNKNOWN_TOKEN")

    matrix.result = SyncResult("s2", (), ())
    matrix.sync = _reject

    try:
        await service.sync_once("tok")
    except AuthError:
        return
    raise AssertionError("AuthError should propagate out of sync_once")


def _turn_span(conversation_id: UUID, seq: int = 5) -> Span:
    return Span(kind=SpanKind.TURN, conversation_id=conversation_id, opened_seq=seq)


async def test_a_spans_line_is_one_event_that_gets_edited(service, matrix, attached) -> None:
    """One line per span, edited in place. A notice per update would make a busy turn unreadable,
    which is the whole point of having a work span rather than progress messages."""
    conversation_id, attachment_id = attached
    span = _turn_span(conversation_id)
    await service.show_span(MATRIX_ROOM, attachment_id, span, "running Bash")
    await settled(service)
    await service.show_span(MATRIX_ROOM, attachment_id, span, "running Read")
    await settled(service)
    await service.show_span(MATRIX_ROOM, attachment_id, span, "running Grep")
    await settled(service)

    assert matrix.notices == [(MATRIX_ROOM, "running Bash")]
    assert matrix.edits == [("$notice-1", "running Read"), ("$notice-1", "running Grep")]


async def test_the_line_is_redacted_when_its_span_retires(service, matrix, attached) -> None:
    conversation_id, attachment_id = attached
    span = _turn_span(conversation_id)
    await service.show_span(MATRIX_ROOM, attachment_id, span, "running Bash")
    await settled(service)

    await service.retire_span(MATRIX_ROOM, attachment_id, span)
    await settled(service)

    assert matrix.redacted == ["$notice-1"]


async def test_a_replica_that_adopts_the_session_edits_the_line_it_inherits(
    service, sync_store, conversations, migrated_identity_store, turns, matrix, migrated_sessions, ledger, attached
) -> None:
    """A span's line outlives the process that posted it. The subject derived from its opening
    event is what a successor resolves through `matrix_revision`, so it edits the line its
    predecessor posted instead of posting a second one beside it."""
    conversation_id, attachment_id = attached
    span = _turn_span(conversation_id)
    await service.show_span(MATRIX_ROOM, attachment_id, span, "running Bash")
    await settled(service)

    successor = _replica(sync_store, conversations, migrated_identity_store, turns, matrix, migrated_sessions, ledger)
    async with successor.pacers.run():
        await successor.show_span(MATRIX_ROOM, attachment_id, span, "running Read")
        await settled(successor)

    assert matrix.notices == [(MATRIX_ROOM, "running Bash")]
    assert matrix.edits == [("$notice-1", "running Read")]


async def test_the_next_turn_opens_a_new_line_rather_than_editing_the_retired_one(service, matrix, attached) -> None:
    """Retiring a span frees nothing to reuse: the next turn is a new span with a new subject, so
    what follows is a create — an edit would address an event the room no longer has."""
    conversation_id, attachment_id = attached
    await service.show_span(MATRIX_ROOM, attachment_id, _turn_span(conversation_id, 5), "running Bash")
    await settled(service)
    await service.retire_span(MATRIX_ROOM, attachment_id, _turn_span(conversation_id, 5))
    await settled(service)

    await service.show_span(MATRIX_ROOM, attachment_id, _turn_span(conversation_id, 9), "running Read")
    await settled(service)

    assert matrix.notices == [(MATRIX_ROOM, "running Bash"), (MATRIX_ROOM, "running Read")]
    assert matrix.edits == []


async def test_retiring_a_span_that_never_showed_anything_does_nothing(service, matrix, attached) -> None:
    """Short turns never create a line, and finishing one must not redact someone else's event."""
    conversation_id, attachment_id = attached
    await service.retire_span(MATRIX_ROOM, attachment_id, _turn_span(conversation_id))
    await settled(service)

    assert matrix.redacted == []


async def test_sealing_a_live_line_is_its_final_edit(service, matrix, attached) -> None:
    """A seal keeps the line in scrollback with its final words, and frees its revision so nothing
    edits it again."""
    conversation_id, attachment_id = attached
    span = Span(kind=SpanKind.SESSION, conversation_id=conversation_id, opened_seq=5)
    await service.show_span(MATRIX_ROOM, attachment_id, span, "provisioning a sandbox")
    await settled(service)

    await service.seal_span(MATRIX_ROOM, attachment_id, span, "the session ended — its sandbox never came up")

    assert matrix.edits == [("$notice-1", "the session ended — its sandbox never came up")]
    assert matrix.redacted == []


async def test_sealing_a_span_with_no_line_posts_the_sealed_notice(service, matrix, attached) -> None:
    """The degenerate seal — no line to edit — is a sealed one-event notice: posted under the
    span's source-derived transaction, so a crash replay is refused rather than doubled."""
    conversation_id, attachment_id = attached
    span = Span(kind=SpanKind.SESSION, conversation_id=conversation_id, opened_seq=7)

    await service.seal_span(MATRIX_ROOM, attachment_id, span, "the session ended — its sandbox never came up")

    assert matrix.notices == [(MATRIX_ROOM, "the session ended — its sandbox never came up")]
    [tag] = matrix.tags
    assert matrix.transactions == [tag.transaction_id()], "source-derived, so a replay is the same transaction"


async def test_a_sealed_source_the_room_already_shows_is_not_posted_again(
    service, matrix, migrated_sessions, attached
) -> None:
    """The replay past the seal: the revision is retired and the room's copy shows the source, so
    repeating the closing event sends nothing at all."""
    conversation_id, attachment_id = attached
    span = Span(kind=SpanKind.SESSION, conversation_id=conversation_id, opened_seq=7)
    await RoomCopy(migrated_sessions).record([_projected("$sealed", attachment_id, conversation_id, 7, ts=1)], [])

    await service.seal_span(MATRIX_ROOM, attachment_id, span, "the session ended — its sandbox never came up")

    assert (matrix.notices, matrix.edits) == ([], [])


async def test_the_takeover_sweep_redacts_lines_no_open_span_accounts_for(service, matrix, attached) -> None:
    """A retirement lost with its replica, or a line under a subject this release no longer
    writes: either way the next leader takes it back, keeping what is still open."""
    conversation_id, attachment_id = attached
    await service.show_span(MATRIX_ROOM, attachment_id, _turn_span(conversation_id, 5), "running Bash")
    await service.show_span(MATRIX_ROOM, attachment_id, _turn_span(conversation_id, 9), "running Read")
    await settled(service)

    await service.retire_stale_spans(MATRIX_ROOM, attachment_id, frozenset({"turn:9"}))
    await settled(service)

    assert matrix.redacted == ["$notice-1"]


async def test_a_reply_says_what_it_is(service, matrix, sync_store, bound_room) -> None:
    """A reply's tag carries the kind and nothing that would publish the same thing twice — and no
    source, so the correspondence reader leaves it alone. Which item an event shows is the outbox
    row's `subject`, which is the transaction it went out under."""
    await service.post_reply(_queued("the answer"))

    [tag] = matrix.tags
    assert (tag.kind, tag.conversation_id) == (RoomEventKind.REPLY, None)


async def test_a_redriven_reply_is_the_same_transaction(service, matrix, sync_store, bound_room) -> None:
    """The homeserver's half of not posting an answer twice: a row re-sent after a failed attempt
    is the same transaction, so Synapse returns the first event rather than making a second."""
    reply = _queued("the answer")

    await service.post_reply(reply)
    await service.post_reply(reply)

    first, second = matrix.transactions
    assert first == second == reply.outbox_id.hex


async def test_a_spans_create_is_its_source_transaction_and_each_edit_a_fresh_one(service, matrix, attached) -> None:
    """The create is derived from the span's source, so one replayed before its revision row
    committed is refused by the homeserver rather than doubled; each edit is a genuinely new event,
    and deriving its transaction would be a way to lose the edit rather than a way to dedupe."""
    conversation_id, attachment_id = attached
    span = _turn_span(conversation_id)
    await service.show_span(MATRIX_ROOM, attachment_id, span, "running Bash")
    await settled(service)
    await service.show_span(MATRIX_ROOM, attachment_id, span, "reading a file")
    await settled(service)

    create, edit = matrix.transactions
    assert create == matrix.tags[0].transaction_id(), "source-derived, so a replayed create is the same send"
    assert edit != create


async def test_a_spans_line_names_its_durable_source_and_not_a_session(service, matrix, attached) -> None:
    """A room event is permanent and federated, so what its tag names has to outlive every session
    that could have produced it — the conversation event that opened the span, under the attachment
    that projected it, which is also what lets `room_copy` hold the editable copy's correspondence."""
    conversation_id, attachment_id = attached
    await service.show_span(MATRIX_ROOM, attachment_id, _turn_span(conversation_id, 5), "running Bash")
    await settled(service)

    [tag] = [tag for tag in matrix.tags if tag.kind is RoomEventKind.STATUS]
    assert tag.source == ConversationEventSource(
        attachment_id=attachment_id, conversation_id=conversation_id, event_seq=5
    )


def _projected(
    event_id: str, attachment_id: UUID, conversation_id: UUID, seq: int, ts: int, room_id: str = MATRIX_ROOM
) -> ProjectedEvent:
    return ProjectedEvent(
        room_id=room_id,
        event_id=event_id,
        source=ConversationEventSource(attachment_id=attachment_id, conversation_id=conversation_id, event_seq=seq),
        origin_server_ts=ts,
        replaces_event_id=None,
    )


@pytest.fixture
async def attached(conversations: ConversationStore, operator_id: UUID, bound_room: str) -> tuple[UUID, UUID]:
    """The bound room's conversation and attachment, which its own events' tags name."""
    binding = await conversations.bind_room(bound_room, operator_id, harness_kind=MATRIX_TEST_HARNESS_KIND)
    return binding.conversation_id, binding.attachment_id


async def test_an_own_echo_is_recorded_and_is_not_input(
    service, matrix, turns, sync_store, migrated_sessions, attached
) -> None:
    """The pass records what the room showed of Haku's own sends, and never offers it as input."""
    conversation_id, attachment_id = attached
    matrix.result = SyncResult("s2", (), (), projected=(_projected("$own", attachment_id, conversation_id, 7, ts=1),))

    await service.sync_once("tok")

    assert await RoomCopy(migrated_sessions).shows(attachment_id, 7)
    assert (await watermark(sync_store), turns.offered) == ("s2", [])


async def test_a_second_live_copy_of_one_source_is_redacted(service, matrix, migrated_sessions, attached) -> None:
    """Duplicate repair: a replay past Synapse's transaction cache posts a second event, and the
    next observation of the pair takes the later copy back — the earlier one is the room's copy."""
    conversation_id, attachment_id = attached
    matrix.result = SyncResult("s2", (), (), projected=(_projected("$first", attachment_id, conversation_id, 7, ts=1),))
    await service.sync_once("tok")

    matrix.result = SyncResult("s3", (), (), projected=(_projected("$again", attachment_id, conversation_id, 7, ts=2),))
    await service.sync_once("tok")

    assert matrix.redacted == ["$again"]


async def test_a_failed_duplicate_redaction_does_not_block_the_pass(
    service, matrix, sync_store, attached, caplog
) -> None:
    """Repair is best effort: a redaction the homeserver refuses is logged loudly, the pass still
    acknowledges its batch, and the store keeps both live rows as the evidence."""
    conversation_id, attachment_id = attached

    async def refuse(token: str, room_id: str, event_id: str, reason: str) -> None:
        raise Error("M_FORBIDDEN")

    matrix.redact = refuse
    matrix.result = SyncResult(
        "s2",
        (),
        (),
        projected=(
            _projected("$first", attachment_id, conversation_id, 7, ts=1),
            _projected("$again", attachment_id, conversation_id, 7, ts=2),
        ),
    )

    with caplog.at_level("ERROR"):
        await service.sync_once("tok")

    assert await watermark(sync_store) == "s2"
    assert "could not redact duplicate" in caplog.text


async def test_an_own_echo_from_an_unserviced_room_reaches_no_row(service, matrix, migrated_sessions, attached) -> None:
    conversation_id, attachment_id = attached
    matrix.result = SyncResult(
        "s2",
        (),
        (),
        projected=(_projected("$stray", attachment_id, conversation_id, 7, ts=1, room_id="!stray:allegedly.works"),),
    )

    await service.sync_once("tok")

    assert not await RoomCopy(migrated_sessions).shows(attachment_id, 7)


async def test_each_kind_of_notice_says_which_it_is(service, matrix, attached) -> None:
    """Three different things, each saying which it is — where msgtype answered "is this
    conversational" only because everything worth excluding happened to be a notice.

    A refusal is not among them any more: it is a recorded row, and the conversation subscriber is
    what says it.
    """
    conversation_id, attachment_id = attached
    await service.project_notice(
        MATRIX_ROOM, attachment_id, "the turn failed — it did", RoomEventKind.LIFECYCLE, conversation_id, 17
    )
    await service.project_notice(
        MATRIX_ROOM, attachment_id, "[sent from another surface] hi", RoomEventKind.NARRATION, conversation_id, 18
    )
    await service.show_span(MATRIX_ROOM, attachment_id, _turn_span(conversation_id), "running Bash")
    await settled(service)

    assert [tag.kind for tag in matrix.tags] == [RoomEventKind.LIFECYCLE, RoomEventKind.NARRATION, RoomEventKind.STATUS]


if __name__ == "__main__":
    pytest_bazel.main()
