"""One sync pass: what gets joined, what reaches the session, and when the watermark moves."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import pytest_bazel
from pydantic import SecretStr

from haku.console.config import MatrixConfig
from haku.console.x.matrix_client import InboundMessage, Invite, MatrixAuthError, SyncResult
from haku.console.x.matrix_sync import MatrixSyncService

USER = "@haku:allegedly.works"
OPERATOR = "@rai:allegedly.works"
ROOM = "!room:allegedly.works"

CONFIG = MatrixConfig(
    homeserver="https://matrix.allegedly.works",
    user_id=USER,
    operator_user_id=OPERATOR,
    operator_subject="authentik-user-id",
)


@dataclass
class _FakeStore:
    """Stands in for the Postgres-backed sync state."""

    token: str | None = None
    next_batch: str | None = None
    saved_batches: list[str] = field(default_factory=list)

    async def load(self, user_id: str):
        return self if (self.token or self.next_batch) else None

    async def save_token(self, user_id: str, token: str) -> None:
        self.token = token

    async def save_batch(self, user_id: str, next_batch: str) -> None:
        self.next_batch = next_batch
        self.saved_batches.append(next_batch)

    @property
    def access_token(self) -> str | None:
        return self.token


@dataclass
class _FakeMatrix:
    """Records what the service asked the homeserver to do."""

    result: SyncResult
    joined: list[str] = field(default_factory=list)
    sent: list[tuple[str, str]] = field(default_factory=list)
    notices: list[tuple[str, str]] = field(default_factory=list)
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

    async def send_text(self, token: str, room_id: str, body: str, txn_id: str) -> str:
        self.sent.append((room_id, body))
        return f"$sent-{len(self.sent)}"

    async def send_notice(self, token: str, room_id: str, body: str, txn_id: str) -> str:
        self.notices.append((room_id, body))
        return f"$notice-{len(self.notices)}"


@dataclass
class _Bound:
    room_id: str


@dataclass
class _FakeConversations:
    """The bound room, or None before any invite has been accepted."""

    room: str | None = None

    async def load(self, user_id: str) -> _Bound | None:
        return _Bound(self.room) if self.room is not None else None

    async def claim_room(self, user_id: str, room_id: str) -> str:
        if self.room is None:
            self.room = room_id
        return self.room


@dataclass
class _FakeTurns:
    """Accepts or refuses a batch, and records what it was offered."""

    accepts: bool = True
    offered: list[list[str]] = field(default_factory=list)

    async def offer(self, messages: Sequence[InboundMessage]) -> bool:
        self.offered.append([message.body for message in messages])
        return self.accepts


@dataclass
class _Harness:
    service: MatrixSyncService
    matrix: _FakeMatrix
    store: _FakeStore
    turns: _FakeTurns


def _harness(
    result: SyncResult, store: _FakeStore | None = None, room: str | None = ROOM, accepts: bool = True
) -> _Harness:
    sync_store = store or _FakeStore()
    turns = _FakeTurns(accepts)
    service = MatrixSyncService(
        CONFIG,
        SecretStr("pw"),
        engine=None,  # type: ignore[arg-type]
        store=sync_store,  # type: ignore[arg-type]
        conversations=_FakeConversations(room),  # type: ignore[arg-type]
        turns=turns,  # type: ignore[arg-type]
    )
    matrix = _FakeMatrix(result)
    service._client = matrix  # type: ignore[assignment]
    return _Harness(service, matrix, sync_store, turns)


def _message(body: str, sender: str = OPERATOR, event_id: str = "$evt") -> InboundMessage:
    return InboundMessage(room_id=ROOM, event_id=event_id, sender=sender, body=body, origin_server_ts=1)


async def test_hands_an_operator_message_to_the_session():
    harness = _harness(SyncResult("s2", (_message("hello"),), ()))

    await harness.service.sync_once("tok")

    assert harness.turns.offered == [["hello"]]


async def test_a_batch_is_offered_as_one_prompt():
    """R2.1 — several messages in one sync response are one turn, not several."""
    harness = _harness(SyncResult("s2", (_message("first", event_id="$a"), _message("second", event_id="$b")), ()))

    await harness.service.sync_once("tok")

    assert harness.turns.offered == [["first", "second"]]


async def test_a_refused_batch_leaves_the_watermark_alone():
    """R2.2 — the homeserver holds the messages and re-delivers them, so nothing is queued here."""
    harness = _harness(SyncResult("s2", (_message("hello"),), ()), accepts=False)

    await harness.service.sync_once("tok")

    assert harness.store.saved_batches == [], "advancing here would drop the message the session refused"
    assert harness.turns.offered == [["hello"]]


async def test_a_refused_batch_says_so_once():
    harness = _harness(SyncResult("s2", (_message("hello"),), ()), accepts=False)

    await harness.service.sync_once("tok")
    await harness.service.sync_once("tok")

    assert len(harness.matrix.notices) == 1, "a held batch is re-offered every pass; saying so every pass is spam"


async def test_joins_an_invite_from_the_operator():
    harness = _harness(SyncResult("s2", (), (Invite(room_id=ROOM, inviter=OPERATOR),)), room=None)

    await harness.service.sync_once("tok")

    assert harness.matrix.joined == [ROOM]


async def test_refuses_a_second_room_and_says_so_in_the_first():
    """R3.6a — joining would put Haku in a room nothing services, which reads as listening."""
    other = "!other:allegedly.works"
    harness = _harness(SyncResult("s2", (), (Invite(room_id=other, inviter=OPERATOR),)))

    await harness.service.sync_once("tok")

    assert harness.matrix.joined == []
    [(room_id, body)] = harness.matrix.notices
    assert room_id == ROOM
    assert "still serving" in body


async def test_leaves_an_invite_from_anybody_else_pending():
    """R3.6 — only the operator's invites are joined; others are surfaced, not acted on."""
    stranger = Invite(room_id="!other:allegedly.works", inviter="@stranger:allegedly.works")
    harness = _harness(SyncResult("s2", (), (stranger,)), room=None)

    await harness.service.sync_once("tok")

    assert harness.matrix.joined == []


async def test_adopts_an_unbound_room_from_operator_traffic():
    """Being in the room already required an operator invite, so a binding can be recovered."""
    stray = InboundMessage("!already-joined:allegedly.works", "$e", OPERATOR, "hi", 1)
    harness = _harness(SyncResult("s2", (stray,), ()), room=None)

    await harness.service.sync_once("tok")

    assert harness.turns.offered == [["hi"]], "the adopting batch is serviced, not dropped"
    [(room_id, body)] = harness.matrix.notices
    assert room_id == "!already-joined:allegedly.works"
    assert "adopted" in body


async def test_does_not_adopt_from_a_sender_who_is_not_the_operator():
    """Adoption inherits R3.6's rule: only the operator can cause Haku to bind a room."""
    stray = InboundMessage("!elsewhere:allegedly.works", "$e", "@stranger:allegedly.works", "hi", 1)
    harness = _harness(SyncResult("s2", (stray,), ()), room=None)

    await harness.service.sync_once("tok")

    assert harness.turns.offered == []
    assert harness.matrix.notices == []


async def test_ignores_messages_from_a_room_that_is_not_the_live_one():
    stray = InboundMessage("!stray:allegedly.works", "$e", OPERATOR, "hi", 1)
    harness = _harness(SyncResult("s2", (stray,), ()))

    await harness.service.sync_once("tok")

    assert harness.turns.offered == []


async def test_reply_posts_the_answer_as_text():
    harness = _harness(SyncResult("s2", (), ()), store=_FakeStore(token="cached"))

    await harness.service.reply("the answer")

    assert harness.matrix.sent == [(ROOM, "the answer")]


async def test_announce_posts_a_notice_into_the_live_room():
    harness = _harness(SyncResult("s2", (), ()), store=_FakeStore(token="cached"))

    await harness.service.announce("provisioning a sandbox")

    assert harness.matrix.notices == [(ROOM, "provisioning a sandbox")]


async def test_announce_is_a_no_op_with_no_room_bound():
    harness = _harness(SyncResult("s2", (), ()), room=None)

    await harness.service.announce("provisioning a sandbox")

    assert harness.matrix.notices == []


async def test_watermark_advances_after_the_batch_is_handled():
    harness = _harness(SyncResult("s2", (_message("hi"),), ()))

    await harness.service.sync_once("tok")

    assert harness.store.saved_batches == ["s2"]
    assert harness.turns.offered, "the batch must be acted on before its watermark is persisted"


async def test_resumes_from_the_stored_watermark():
    harness = _harness(SyncResult("s5", (), ()), store=_FakeStore(next_batch="s4"))

    await harness.service.sync_once("tok")

    assert harness.matrix.since == "s4"


async def test_reuses_a_valid_cached_token():
    """Synapse rate-limits /login, so a working token must not be re-minted (R10.3a)."""
    harness = _harness(SyncResult("s2", (), ()), store=_FakeStore(token="cached"))

    assert await harness.service._token() == "cached"
    assert harness.matrix.logins == 0


async def test_logs_in_again_when_the_cached_token_is_rejected():
    harness = _harness(SyncResult("s2", (), ()), store=_FakeStore(token="stale"))
    harness.matrix.token_valid = False

    assert await harness.service._token() == "fresh-token"
    assert (harness.matrix.logins, harness.store.token) == (1, "fresh-token")


async def test_auth_error_surfaces_so_the_loop_can_re_login():
    """The loop distinguishes a rejected token from a transport failure."""

    async def _reject(token: str, since: str | None) -> SyncResult:
        raise MatrixAuthError("M_UNKNOWN_TOKEN")

    harness = _harness(SyncResult("s2", (), ()))
    harness.matrix.sync = _reject  # type: ignore[method-assign]

    try:
        await harness.service.sync_once("tok")
    except MatrixAuthError:
        return
    raise AssertionError("MatrixAuthError should propagate out of sync_once")


if __name__ == "__main__":
    pytest_bazel.main()
