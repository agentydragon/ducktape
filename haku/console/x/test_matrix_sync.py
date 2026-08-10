"""One sync pass: what gets joined, what reaches the session, and when the watermark moves."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, cast

import pytest
import pytest_bazel
from pydantic import SecretStr

from haku.console.x.conftest import MATRIX_CONFIG, MATRIX_OPERATOR, MATRIX_ROOM, MATRIX_USER
from haku.console.x.matrix_client import InboundMessage, Invite, MatrixAuthError, SyncResult
from haku.console.x.matrix_sync import MatrixSyncService, MatrixSyncStore


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
def service(sync_store, conversations, turns, matrix) -> MatrixSyncService:
    service = MatrixSyncService(
        MATRIX_CONFIG,
        SecretStr("pw"),
        engine=cast(Any, None),  # only `run()` takes the advisory lock; these drive one pass
        store=sync_store,
        conversations=conversations,
        turns=cast(Any, turns),
    )
    service._client = cast(Any, matrix)
    return service


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


def _message(body: str, sender: str = MATRIX_OPERATOR, event_id: str = "$evt") -> InboundMessage:
    return InboundMessage(room_id=MATRIX_ROOM, event_id=event_id, sender=sender, body=body, origin_server_ts=1)


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

    await service.sync_once("tok")

    assert await watermark(sync_store) is None, "advancing here would drop the message the session refused"
    assert turns.offered == [["hello"]]


async def test_a_refused_batch_says_so_once(service, matrix, turns, bound_room):
    matrix.result = SyncResult("s2", (_message("hello"),), ())
    turns.accepts = False

    await service.sync_once("tok")
    await service.sync_once("tok")

    assert len(matrix.notices) == 1, "a held batch is re-offered every pass; saying so every pass is spam"


async def test_joins_an_invite_from_the_operator(service, matrix):
    matrix.result = SyncResult("s2", (), (Invite(room_id=MATRIX_ROOM, inviter=MATRIX_OPERATOR),))

    await service.sync_once("tok")

    assert matrix.joined == [MATRIX_ROOM]


async def test_refuses_a_second_room_and_says_so_in_the_first(service, matrix, bound_room):
    """R3.6a — joining would put Haku in a room nothing services, which reads as listening."""
    other = "!other:allegedly.works"
    matrix.result = SyncResult("s2", (), (Invite(room_id=other, inviter=MATRIX_OPERATOR),))

    await service.sync_once("tok")

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

    await service.reply("the answer")

    assert matrix.sent == [(MATRIX_ROOM, "the answer")]


async def test_announce_posts_a_notice_into_the_live_room(service, matrix, sync_store, bound_room):
    matrix.result = SyncResult("s2", (), ())
    await sync_store.save_token(MATRIX_USER, "cached")

    await service.announce("provisioning a sandbox")

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


if __name__ == "__main__":
    pytest_bazel.main()
