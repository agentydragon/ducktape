"""One sync pass: what gets joined, what reaches the session, and when the watermark moves."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from pydantic import SecretStr

from haku.console.x.conftest import MATRIX_CONFIG, MATRIX_OPERATOR, MATRIX_ROOM, MATRIX_USER
from haku.console.x.matrix_client import (
    EventTag,
    InboundMessage,
    Invite,
    MatrixAuthError,
    RoomEventKind,
    SyncResult,
    UnmappableEvent,
)
from haku.console.x.matrix_pacer import RoomPacer
from haku.console.x.matrix_sync import MatrixSyncService, MatrixSyncStore


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
    """Accepts or refuses a batch, and records what it was offered."""

    accepts: bool = True
    offered: list[list[str]] = field(default_factory=list)

    async def offer(self, messages: Sequence[InboundMessage]) -> bool:
        self.offered.append([message.body for message in messages])
        return self.accepts


@pytest.fixture
def sync_store(migrated_sessions) -> MatrixSyncStore:
    return MatrixSyncStore(migrated_sessions)


@pytest.fixture
def turns() -> _FakeTurns:
    return _FakeTurns()


@pytest.fixture
def matrix() -> _FakeMatrix:
    """The homeserver. Tests set `matrix.result` to the sync response under test."""
    return _FakeMatrix(SyncResult("s2", (), ()))


@pytest.fixture
async def service(sync_store, conversations, turns, matrix):
    """The service with its outbound queue running, because every send goes through it.

    Unthrottled: what the real budget is and how it is spent is `test_matrix_pacer`'s subject,
    and giving these tests the room's true rate would make each of them wait five seconds per
    send to assert something that is not about waiting.
    """
    service = MatrixSyncService(
        MATRIX_CONFIG,
        SecretStr("pw"),
        engine=cast(Any, None),  # only `run()` takes the advisory lock; these drive one pass
        store=sync_store,
        conversations=conversations,
        turns=cast(Any, turns),
    )
    service._client = cast(Any, matrix)
    service.pacer = RoomPacer(sends_per_second=1e6, burst=1_000)
    async with service.pacer.run():
        yield service


async def settled(service: MatrixSyncService) -> None:
    """Wait for what the service queued to actually reach the homeserver."""
    await service.pacer.flush()


@pytest.fixture
async def bound_room(conversations) -> str:
    """Most tests start from a room already bound; the adoption/invite ones do not use this."""
    await conversations.claim_room(MATRIX_USER, MATRIX_ROOM)
    return MATRIX_ROOM


async def watermark(store: MatrixSyncStore) -> str | None:
    state = await store.load(MATRIX_USER)
    return state.next_batch if state is not None else None


async def cached_token(store: MatrixSyncStore) -> str | None:
    state = await store.load(MATRIX_USER)
    return state.access_token if state is not None else None


SESSION = UUID("11111111-2222-3333-4444-555555555555")


def _message(body: str, sender: str = MATRIX_OPERATOR, event_id: str = "$evt") -> InboundMessage:
    return InboundMessage(room_id=MATRIX_ROOM, event_id=event_id, sender=sender, body=body, origin_server_ts=1)


def _unreadable(msgtype: str = "m.image", event_id: str = "$img", room_id: str = MATRIX_ROOM) -> UnmappableEvent:
    return UnmappableEvent(room_id=room_id, event_id=event_id, sender=MATRIX_OPERATOR, msgtype=msgtype)


async def test_hands_an_operator_message_to_the_session(service, matrix, turns, bound_room):
    matrix.result = SyncResult("s2", (_message("hello"),), ())

    await service.sync_once("tok")

    assert turns.offered == [["hello"]]


async def test_a_batch_is_offered_as_one_prompt(service, matrix, turns, bound_room):
    """R2.1 — several messages in one sync response are one turn, not several."""
    matrix.result = SyncResult("s2", (_message("first", event_id="$a"), _message("second", event_id="$b")), ())

    await service.sync_once("tok")

    assert turns.offered == [["first", "second"]]


async def test_a_refused_batch_leaves_the_watermark_alone(service, matrix, turns, sync_store, bound_room):
    """R2.2 — the homeserver holds the messages and re-delivers them, so nothing is queued here."""
    matrix.result = SyncResult("s2", (_message("hello"),), ())
    turns.accepts = False

    made_progress = await service.sync_once("tok")

    assert made_progress is False, "the caller backs off on a refusal — see REFUSED_BATCH_BACKOFF"
    assert await watermark(sync_store) is None, "advancing here would drop the message the session refused"
    assert turns.offered == [["hello"]]


async def test_a_refused_batch_says_so_once(service, matrix, turns, bound_room):
    matrix.result = SyncResult("s2", (_message("hello"),), ())
    turns.accepts = False

    await service.sync_once("tok")
    await service.sync_once("tok")
    await settled(service)

    assert len(matrix.notices) == 1, "a held batch is re-offered every pass; saying so every pass is spam"


async def test_an_unreadable_event_is_announced_and_the_batch_moves_on(service, matrix, turns, sync_store, bound_room):
    """R1.6, the half a refusal cannot serve. Nothing about a sent `m.image` will ever change, so
    holding the batch for it would wedge ingress on one screenshot forever; the operator is told
    in the room they sent it to, and the watermark advances."""
    matrix.result = SyncResult("s2", (), (), (_unreadable(),))

    made_progress = await service.sync_once("tok")
    await settled(service)

    assert made_progress is True
    assert await watermark(sync_store) == "s2"
    assert turns.offered == [], "there is no prose in this batch to hand over"
    [(room_id, body)] = matrix.notices
    assert room_id == MATRIX_ROOM
    assert "m.image" in body
    assert "cannot read" in body
    assert [tag.kind for tag in matrix.tags] == [RoomEventKind.UNREADABLE]


async def test_the_text_of_a_mixed_batch_is_serviced_and_the_rest_announced(service, matrix, turns, bound_room):
    """A "look at this" alongside a screenshot: the sentence still starts a turn."""
    matrix.result = SyncResult("s2", (_message("look at this"),), (), (_unreadable(),))

    await service.sync_once("tok")
    await settled(service)

    assert turns.offered == [["look at this"]]
    [(_, body)] = matrix.notices
    assert "m.image" in body


async def test_an_unreadable_event_is_announced_once_the_batch_is_taken_and_not_before(
    service, matrix, turns, bound_room
):
    """A refused batch is re-offered every pass. Announcing the attachment before the prose next
    to it has been accepted would repeat the announcement for the length of the turn in flight."""
    matrix.result = SyncResult("s2", (_message("look at this"),), (), (_unreadable(),))
    turns.accepts = False

    await service.sync_once("tok")
    await service.sync_once("tok")
    await settled(service)

    assert [body for _, body in matrix.notices] == ["holding 1 message(s) until Haku is ready"], (
        "the attachment is not announced while its batch is still being re-offered"
    )

    turns.accepts = True
    await service.sync_once("tok")
    await settled(service)

    [(_, announced)] = matrix.notices[1:]
    assert "m.image" in announced


async def test_an_unreadable_event_from_an_unserviced_room_is_not_announced(service, matrix, bound_room):
    """The notice goes to the room the operator sent it to, and only the bound room is that
    room (R3.6a)."""
    matrix.result = SyncResult("s2", (), (), (_unreadable(room_id="!stray:allegedly.works"),))

    await service.sync_once("tok")
    await settled(service)

    assert matrix.notices == []


async def test_joins_an_invite_from_the_operator(service, matrix):
    matrix.result = SyncResult("s2", (), (Invite(room_id=MATRIX_ROOM, inviter=MATRIX_OPERATOR),))

    await service.sync_once("tok")

    assert matrix.joined == [MATRIX_ROOM]


async def test_refuses_a_second_room_and_says_so_in_the_first(service, matrix, bound_room):
    """R3.6a — joining would put Haku in a room nothing services, which reads as listening."""
    other = "!other:allegedly.works"
    matrix.result = SyncResult("s2", (), (Invite(room_id=other, inviter=MATRIX_OPERATOR),))

    await service.sync_once("tok")

    await settled(service)
    assert matrix.joined == []
    [(room_id, body)] = matrix.notices
    assert room_id == MATRIX_ROOM
    assert "still serving" in body


async def test_leaves_an_invite_from_anybody_else_pending(service, matrix):
    """R3.6 — only the operator's invites are joined; others are surfaced, not acted on."""
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
    """Adoption inherits R3.6's rule: only the operator can cause Haku to bind a room."""
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


async def test_reply_posts_the_answer_as_text(service, matrix, sync_store, bound_room):
    matrix.result = SyncResult("s2", (), ())
    await sync_store.save_token(MATRIX_USER, "cached")

    await service.reply("the answer", EventTag(kind=RoomEventKind.REPLY, session_id=SESSION))
    await settled(service)

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


async def test_watermark_advances_after_the_batch_is_handled(service, matrix, turns, sync_store, bound_room):
    matrix.result = SyncResult("s2", (_message("hi"),), ())

    await service.sync_once("tok")

    assert await watermark(sync_store) == "s2"
    assert turns.offered, "the batch must be acted on before its watermark is persisted"


async def test_resumes_from_the_stored_watermark(service, matrix, sync_store, bound_room):
    matrix.result = SyncResult("s5", (), ())
    await sync_store.save_batch(MATRIX_USER, "s4")

    await service.sync_once("tok")

    assert matrix.since == "s4"


async def test_reuses_a_valid_cached_token(service, matrix, sync_store, bound_room):
    """Synapse rate-limits /login, so a working token must not be re-minted (R10.3a)."""
    matrix.result = SyncResult("s2", (), ())
    await sync_store.save_token(MATRIX_USER, "cached")

    assert await service._token() == "cached"
    assert matrix.logins == 0


async def test_logs_in_again_when_the_cached_token_is_rejected(service, matrix, sync_store, bound_room):
    matrix.result = SyncResult("s2", (), ())
    await sync_store.save_token(MATRIX_USER, "stale")
    matrix.token_valid = False

    assert await service._token() == "fresh-token"
    assert (matrix.logins, await cached_token(sync_store)) == (1, "fresh-token")


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
    """R6.5. A notice per update would make a busy turn unreadable, which is the whole point
    of having a status line rather than progress messages."""
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
    and they belong to the caller (`claude_chat._TurnStatus`).

    Declining here used to lose the update outright — the driver had already recorded it as
    shown, so it never offered it again and the room read the older state for the rest of the
    turn. The floor is still there; it is just where the deferral can be remembered.
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


async def test_clearing_a_turn_that_never_showed_anything_does_nothing(service, matrix, bound_room) -> None:
    """Short turns never create a line, and finishing one must not redact someone else's event."""
    await service.clear_status()
    await settled(service)

    assert matrix.redacted == []


async def test_a_reply_says_which_transcript_row_it_is(service, matrix, sync_store, bound_room) -> None:
    """The statement that replaces a guess. Which message an event shows used to be answerable
    only by matching order and timing against the transcript; stage 4's dedupe needs it to be a
    lookup, and that is cheap only if the event said so when it was sent."""
    message_id = UUID("99999999-8888-7777-6666-555555555555")
    await service.reply(
        "the answer",
        EventTag(kind=RoomEventKind.REPLY, session_id=SESSION, message_id=message_id, agent_message_id="msg_01abc"),
    )
    await settled(service)

    [tag] = matrix.tags
    assert (tag.kind, tag.session_id, tag.message_id, tag.agent_message_id) == (
        RoomEventKind.REPLY,
        SESSION,
        message_id,
        "msg_01abc",
    )


async def test_a_replayed_reply_is_the_same_transaction(service, matrix, sync_store, bound_room) -> None:
    """The homeserver's half of not posting an answer twice: the same transcript row re-sent is
    the same transaction, so Synapse returns the first event rather than making a second."""
    message_id = uuid4()
    tag = EventTag(kind=RoomEventKind.REPLY, session_id=SESSION, message_id=message_id)
    await service.reply("the answer", tag)
    await service.reply("the answer", tag)
    await settled(service)

    first, second = matrix.transactions
    assert first == second == message_id.hex


async def test_a_status_edit_is_a_new_transaction_every_time(service, matrix, bound_room) -> None:
    """The other half of the rule: a line with no row to name is a genuinely new event each time,
    and deriving its transaction would be a way to lose the edit rather than a way to dedupe."""
    await service.show_status("running Bash", SESSION)
    await service.show_status("reading a file", SESSION)
    await settled(service)

    assert len(set(matrix.transactions)) == len(matrix.transactions) == 2


async def test_each_kind_of_notice_says_which_it_is(service, matrix, turns, bound_room) -> None:
    """Msgtype answered "is this conversational" only because everything worth excluding happened
    to be a notice. These are four different things and now say so."""
    matrix.result = SyncResult("s2", (_message("hello"),), ())
    turns.accepts = False
    await service.sync_once("tok")
    await service.announce("provisioning a sandbox")
    await service.announce("cloning haku-state", RoomEventKind.NARRATION)
    await service.show_status("running Bash", SESSION)
    await settled(service)

    assert [tag.kind for tag in matrix.tags] == [
        RoomEventKind.HOLDING,
        RoomEventKind.LIFECYCLE,
        RoomEventKind.NARRATION,
        RoomEventKind.STATUS,
    ]


if __name__ == "__main__":
    pytest_bazel.main()
