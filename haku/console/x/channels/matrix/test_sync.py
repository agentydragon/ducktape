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
from pydantic import SecretStr
from sqlalchemy import select

from haku.console.chat_models import (
    SPA_ORIGIN,
    AuthoredEventKind,
    ItemType,
    MatrixOrigin,
    PromptRejection,
    StoredEventKind,
)
from haku.console.database_schema import ConversationEvent
from haku.console.x.channels.matrix.client import (
    EventTag,
    InboundMessage,
    Invite,
    MatrixAuthError,
    RoomEventKind,
    SyncResult,
    UnmappableEvent,
)
from haku.console.x.channels.matrix.conftest import MATRIX_CONFIG, MATRIX_OPERATOR, MATRIX_ROOM, MATRIX_USER
from haku.console.x.channels.matrix.conversation import (
    Admission,
    ConversationFacts,
    MatrixConversationStore,
    PromptAccepted,
    PromptRejected,
    RoomTranscript,
)
from haku.console.x.channels.matrix.ingress_ledger import IngressLedger
from haku.console.x.channels.matrix.outbox import PendingReply
from haku.console.x.channels.matrix.pacer import RoomPacer
from haku.console.x.channels.matrix.revisions import RevisionLog
from haku.console.x.channels.matrix.sync import MatrixSyncService, MatrixSyncStore
from haku.console.x.conversation_events import FrameRange, ItemSegment, MessageCompleted, MessageStarted, OpenRef
from haku.console.x.session_events import PromptRejectedBody, UnreadableInputBody
from haku.console.x.session_store import BridgeAuthentication, SessionStore


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

    What it hands back are bodies and the conversation they belong to, because an authored row's
    position is allocated under that conversation's lock — so what the loop does with them is append
    them where it moves the watermark. `conversation_id = None` is the room bound to nothing, where
    there is nowhere to record and nowhere to say it either.
    """

    conversation_id: UUID | None
    session_id: UUID | None = None
    accepts: bool = True
    reason: PromptRejection = PromptRejection.TURN_IN_FLIGHT
    offered: list[list[str]] = field(default_factory=list)

    async def offer(self, messages: Sequence[InboundMessage]) -> Admission:
        self.offered.append([message.body for message in messages])
        if self.accepts:
            return PromptAccepted(item_id=uuid4())
        return PromptRejected(
            reason=self.reason,
            facts=self._facts(PromptRejectedBody(reason=self.reason, text="\n".join(self.offered[-1]))),
        )

    async def unreadable(self, events: Sequence[UnmappableEvent]) -> ConversationFacts | None:
        return self._facts(*(UnreadableInputBody(media_type=event.msgtype) for event in events))

    def _facts(self, *bodies: PromptRejectedBody | UnreadableInputBody) -> ConversationFacts | None:
        if self.conversation_id is None:
            return None
        return ConversationFacts(conversation_id=self.conversation_id, session_id=self.session_id, bodies=tuple(bodies))


@pytest.fixture
def sync_store(migrated_sessions) -> MatrixSyncStore:
    return MatrixSyncStore(migrated_sessions)


@pytest.fixture
async def turns(chat_store: SessionStore, operator_id: UUID) -> _FakeTurns:
    """Ingress over a real conversation, since what it hands the loop is appended to one."""
    view, _ = await chat_store.create(operator_id)
    return _FakeTurns(await chat_store.conversation_of(view.session_id), view.session_id)


@pytest.fixture
def matrix() -> _FakeMatrix:
    """The homeserver. Tests set `matrix.result` to the sync response under test."""
    return _FakeMatrix(SyncResult("s2", (), ()))


@pytest.fixture
def transcript(migrated_sessions) -> RoomTranscript:
    return RoomTranscript(migrated_sessions)


def _replica(
    sync_store, conversations, identities, turns, transcript, matrix, migrated_sessions, ledger
) -> MatrixSyncService:
    """One console replica's sync service, unthrottled.

    The real budget is `test_pacer`'s subject; at the room's true rate each of these would wait
    five seconds per send to assert something that is not about waiting.
    """
    service = MatrixSyncService(
        MATRIX_CONFIG,
        SecretStr("pw"),
        engine=cast(Any, None),  # only `run()` takes the advisory lock; these drive one pass
        store=sync_store,
        conversations=conversations,
        identities=identities,
        turns=cast(Any, turns),
        transcript=transcript,
        # Answers are outbox rows, drained by a task `run()` starts; these tests drive one sync
        # pass and assert the narration, which never touches the table (`test_outbox` does).
        outbox=cast(Any, None),
        revisions=RevisionLog(migrated_sessions),
        ledger=ledger,
    )
    service._client = cast(Any, matrix)
    service.pacer = RoomPacer(sends_per_second=1e6, burst=1_000)
    return service


@pytest.fixture
async def service(
    sync_store, conversations, migrated_identity_store, turns, transcript, matrix, migrated_sessions, ledger
):
    """The service with its outbound queue running, because every send goes through it."""
    service = _replica(
        sync_store, conversations, migrated_identity_store, turns, transcript, matrix, migrated_sessions, ledger
    )
    async with service.pacer.run():
        yield service


async def settled(service: MatrixSyncService) -> None:
    """Wait for what the service queued to actually reach the homeserver."""
    await service.pacer.flush()


@pytest.fixture
async def bound_room(conversations: MatrixConversationStore, operator_id: UUID) -> str:
    """Most tests start from a room already bound; the adoption/invite ones do not use this.

    Binding is what attaches it, so this is also the row the status line's own event id hangs off.
    """
    return (await conversations.bind_room(MATRIX_ROOM, operator_id)).room_id


async def watermark(store: MatrixSyncStore) -> str | None:
    return await store.watermark(MATRIX_USER)


async def recorded(sessions) -> list[tuple[StoredEventKind, dict[str, Any]]]:
    """The authored rows a pass wrote, oldest first — the durable half of what the room hears."""
    async with sessions() as db:
        rows = (await db.scalars(select(ConversationEvent).order_by(ConversationEvent.event_seq))).all()
    return [(row.kind, row.body) for row in rows]


SESSION = UUID("11111111-2222-3333-4444-555555555555")


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
    chat_store: SessionStore, operator_id: UUID, ledger: IngressLedger, event_id: str, body: str
) -> UUID:
    """A prompt in the record carrying *event_id*, as an accepted batch leaves one behind."""
    view, token = await chat_store.create(operator_id)
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(
        operator_id,
        view.session_id,
        f"[{event_id}] {body}",
        MatrixOrigin(address=MATRIX_ROOM, refs=(event_id,)),
        ledger.carrying((event_id,)),
    )
    return view.session_id


async def test_a_re_delivered_message_is_dropped_from_the_batch(
    service, matrix, turns, sync_store, chat_store, operator_id, ledger, bound_room
):
    """The crash this closes: the prompt committed, the watermark did not, and `/sync` hands the
    same event back. Offering it again would ask twice — and be refused, since the first copy is
    still queued, so the room would report a message as undelivered that the session is about to
    answer."""
    await carried_prompt(chat_store, operator_id, ledger, "$a", "hello")
    matrix.result = SyncResult("s2", (_message("hello", event_id="$a"),), ())

    await service.sync_once("tok")
    await settled(service)

    assert turns.offered == []
    assert await watermark(sync_store) == "s2"
    assert matrix.notices == []


async def test_only_the_re_delivered_half_of_a_batch_is_dropped(
    service, matrix, turns, chat_store, operator_id, ledger, bound_room
):
    """A restart can land the crashed batch and what was said since in one response."""
    await carried_prompt(chat_store, operator_id, ledger, "$a", "hello")
    matrix.result = SyncResult("s2", (_message("hello", event_id="$a"), _message("and this", event_id="$b")), ())

    await service.sync_once("tok")

    assert turns.offered == [["and this"]]


async def test_a_rejected_batch_is_acknowledged_and_recorded_in_one_go(
    service, matrix, turns, sync_store, migrated_sessions, bound_room
):
    """A prompt the session will not take is rejected, not held.

    The watermark covers the batch, so the homeserver will not offer it again, and the only
    surviving copy of what was said is the row written beside that watermark. The room hears about
    it from that row (`room_subscription.notice`), so this pass says nothing itself.
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
        conversation_id=uuid4(), session_id=None, bodies=(UnreadableInputBody(media_type="m.image"),)
    )

    with pytest.raises(KeyError):
        await sync_store.advance(MATRIX_USER, "s2", orphan)

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
    no session named because there was none. `RoomNotices` says it from that row, which is why this
    pass says nothing itself.
    """
    matrix.result = SyncResult("s2", (_message("hello"),), ())
    turns.accepts = False
    turns.reason = PromptRejection.NO_SESSION
    turns.session_id = None

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
    """Only the bound room is serviced, so a stray room's event is neither recorded nor said."""
    matrix.result = SyncResult("s2", (), (), (_unreadable(room_id="!stray:allegedly.works"),))

    await service.sync_once("tok")
    await settled(service)

    assert (matrix.notices, await recorded(migrated_sessions)) == ([], [])


async def test_joins_an_invite_from_the_operator(service, matrix):
    matrix.result = SyncResult("s2", (), (Invite(room_id=MATRIX_ROOM, inviter=MATRIX_OPERATOR),))

    await service.sync_once("tok")

    assert matrix.joined == [MATRIX_ROOM]


async def test_refuses_a_second_room_and_says_so_in_the_first(service, matrix, bound_room):
    """Joining would put Haku in a room nothing services, which reads as listening."""
    other = "!other:allegedly.works"
    matrix.result = SyncResult("s2", (), (Invite(room_id=other, inviter=MATRIX_OPERATOR),))

    await service.sync_once("tok")

    await settled(service)
    assert matrix.joined == []
    [(room_id, body)] = matrix.notices
    assert room_id == MATRIX_ROOM
    assert "still serving" in body


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


async def test_ignores_messages_from_a_room_that_is_not_the_live_one(service, matrix, turns, bound_room):
    stray = InboundMessage("!stray:allegedly.works", "$e", MATRIX_OPERATOR, "hi", 1)
    matrix.result = SyncResult("s2", (stray,), ())

    await service.sync_once("tok")

    assert turns.offered == []


async def test_posting_a_queued_reply_says_it_as_text(service, matrix, sync_store, bound_room):
    matrix.result = SyncResult("s2", (), ())
    await sync_store.save_token(MATRIX_USER, "cached")

    await service.post_reply(_queued("the answer"))

    assert matrix.sent == [(MATRIX_ROOM, "the answer")]


async def test_announce_posts_a_notice_into_the_live_room(service, matrix, sync_store, bound_room):
    matrix.result = SyncResult("s2", (), ())
    await sync_store.save_token(MATRIX_USER, "cached")

    await service.announce("provisioning a sandbox")
    await settled(service)

    assert matrix.notices == [(MATRIX_ROOM, "provisioning a sandbox")]


async def test_announce_is_a_no_op_with_no_room_bound(service, matrix):
    matrix.result = SyncResult("s2", (), ())

    await service.announce("provisioning a sandbox")

    assert matrix.notices == []


async def test_a_quiet_batch_advances_the_watermark(service, matrix, sync_store, bound_room):
    matrix.result = SyncResult("s2", (), ())

    await service.sync_once("tok")

    assert await watermark(sync_store) == "s2"


async def test_a_batch_handed_over_is_acknowledged_at_once(service, matrix, turns, sync_store, bound_room):
    """Acceptance is the acknowledgement, and what it costs is that a prompt whose session ends
    before claiming it is not offered again — what answers it is the replacement session being
    handed the transcript it is already in (`session.RoomTranscript.recent`)."""
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
        raise MatrixAuthError("M_UNKNOWN_TOKEN")

    matrix.result = SyncResult("s2", (), ())
    matrix.sync = _reject

    try:
        await service.sync_once("tok")
    except MatrixAuthError:
        return
    raise AssertionError("MatrixAuthError should propagate out of sync_once")


async def test_the_turn_status_is_one_line_that_gets_edited(service, matrix, bound_room) -> None:
    """One line per turn, edited in place. A notice per update would make a busy turn unreadable,
    which is the whole point of having a status line rather than progress messages."""
    await service.show_status("running Bash")
    await settled(service)
    await service.show_status("running Read")
    await settled(service)

    assert matrix.notices == [(bound_room, "running Bash")]
    assert matrix.edits == [("$notice-1", "running Read")]


async def test_a_repeated_state_is_not_resent(service, matrix, bound_room) -> None:
    await service.show_status("running Bash")
    await service.show_status("running Bash")
    await settled(service)

    assert matrix.edits == []


async def test_every_state_it_is_given_reaches_the_line(service, matrix, bound_room) -> None:
    """Idempotent, not paced: what the line should say and when it may change are one decision,
    and they belong to the caller (`room_status.TurnStatus`). Declining here would lose the update
    outright, since the driver has already recorded it as shown.
    """
    await service.show_status("running Bash")
    await settled(service)
    await service.show_status("running Read")
    await settled(service)
    await service.show_status("running Grep")
    await settled(service)

    assert matrix.edits == [("$notice-1", "running Read"), ("$notice-1", "running Grep")]


async def test_the_line_is_redacted_when_the_turn_ends(service, matrix, bound_room) -> None:
    await service.show_status("running Bash")
    await settled(service)

    await service.clear_status()
    await settled(service)

    assert matrix.redacted == ["$notice-1"]


async def test_a_replica_that_adopts_the_session_edits_the_line_it_inherits(
    service,
    sync_store,
    conversations,
    migrated_identity_store,
    turns,
    transcript,
    matrix,
    migrated_sessions,
    ledger,
    bound_room,
) -> None:
    """The status line outlives the process that posted it. Whichever replica holds the session's
    lease drives the line, so one starting with an empty process would post a second line beside
    its predecessor's and leave that one saying `running Bash` forever."""
    await service.show_status("running Bash")
    await settled(service)

    successor = _replica(
        sync_store, conversations, migrated_identity_store, turns, transcript, matrix, migrated_sessions, ledger
    )
    async with successor.pacer.run():
        await successor.show_status("running Read")
        await settled(successor)

    assert matrix.notices == [(bound_room, "running Bash")]
    assert matrix.edits == [("$notice-1", "running Read")]


async def test_the_next_turn_opens_a_new_line_rather_than_editing_the_redacted_one(service, matrix, bound_room) -> None:
    """Retiring the line frees its subject, so what follows is a create — an edit would address an
    event the room no longer has."""
    await service.show_status("running Bash")
    await settled(service)
    await service.clear_status()
    await settled(service)

    await service.show_status("running Read")
    await settled(service)

    assert matrix.notices == [(bound_room, "running Bash"), (bound_room, "running Read")]
    assert matrix.edits == []


async def test_clearing_a_turn_that_never_showed_anything_does_nothing(service, matrix, bound_room) -> None:
    """Short turns never create a line, and finishing one must not redact someone else's event."""
    await service.clear_status()
    await settled(service)

    assert matrix.redacted == []


async def test_a_reply_says_what_it_is(service, matrix, sync_store, bound_room) -> None:
    """The tag is write-only and for a person reading the room's event source, so what it carries is
    the kind and nothing that would publish the same thing twice. Which item an event shows is the
    outbox row's `subject`, which is the transaction it went out under."""
    await service.post_reply(_queued("the answer"))

    [tag] = matrix.tags
    assert (tag.kind, tag.session_id) == (RoomEventKind.REPLY, None)


async def test_a_redriven_reply_is_the_same_transaction(service, matrix, sync_store, bound_room) -> None:
    """The homeserver's half of not posting an answer twice: a row re-sent after a failed attempt
    is the same transaction, so Synapse returns the first event rather than making a second."""
    reply = _queued("the answer")

    await service.post_reply(reply)
    await service.post_reply(reply)

    first, second = matrix.transactions
    assert first == second == reply.outbox_id.hex


async def test_a_status_edit_is_a_new_transaction_every_time(service, matrix, bound_room) -> None:
    """The other half of the rule: a line with no row to name is a genuinely new event each time,
    and deriving its transaction would be a way to lose the edit rather than a way to dedupe."""
    await service.show_status("running Bash", SESSION)
    await service.show_status("reading a file", SESSION)
    await settled(service)

    assert len(set(matrix.transactions)) == len(matrix.transactions) == 2


async def test_each_kind_of_notice_says_which_it_is(service, matrix, bound_room) -> None:
    """Three different things, each saying which it is — where msgtype answered "is this
    conversational" only because everything worth excluding happened to be a notice.

    A refusal is not among them any more: it is a recorded row, and `RoomNotices` is what says it.
    """
    await service.announce("provisioning a sandbox")
    await service.announce("cloning haku-state", RoomEventKind.NARRATION)
    await service.show_status("running Bash", SESSION)
    await settled(service)

    assert [tag.kind for tag in matrix.tags] == [RoomEventKind.LIFECYCLE, RoomEventKind.NARRATION, RoomEventKind.STATUS]


async def test_history_is_read_from_our_record_and_not_from_the_homeserver(
    service, matrix, chat_store, conversations, operator_id, bound_room
) -> None:
    """What a replacement session is told it said, at the seam.

    An item type becomes an MXID here because that is the per-channel half of the answer: the record
    knows what kind of item it was, and only the channel knows what to call whoever produced it.
    `matrix` is asserted untouched because "we asked the homeserver" and "we asked ourselves" are
    otherwise indistinguishable from the outside.
    """
    view, token = await chat_store.create(
        operator_id, conversation_id=(await conversations.bind_room(MATRIX_ROOM, operator_id)).conversation_id
    )
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, view.session_id, "hi", SPA_ORIGIN)
    start = await chat_store.next_prompt(view.session_id)
    assert start is not None
    where = FrameRange(1, 1)
    await chat_store.apply_frame(
        view.session_id,
        start.turn_id,
        1,
        [
            MessageStarted(provenance=where),
            ItemSegment(item=OpenRef(item_type=ItemType.MESSAGE), text="hello", provenance=where),
            MessageCompleted(backend_item_id=None, provenance=where),
        ],
    )

    said = await service.recent_history(uuid4(), 20)

    assert [(message.sender, message.body) for message in said] == [(MATRIX_OPERATOR, "hi"), (MATRIX_USER, "hello")]
    assert matrix.since is None, "the homeserver was not asked anything to answer this"


async def test_a_room_nothing_has_been_recorded_for_has_no_history(service, bound_room) -> None:
    """A first-ever session and one whose room was just bound read the same, and both correctly."""
    assert await service.recent_history(uuid4(), 20) == ()


if __name__ == "__main__":
    pytest_bazel.main()
