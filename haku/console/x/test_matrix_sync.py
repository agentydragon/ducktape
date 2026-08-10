"""Behaviour of one sync pass: what gets joined, what gets echoed, when the watermark moves."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest_bazel
from pydantic import SecretStr

from haku.console.config import MatrixConfig
from haku.console.x.matrix_client import InboundMessage, Invite, MatrixAuthError, SyncResult
from haku.console.x.matrix_sync import MatrixSyncService

USER = "@haku:allegedly.works"
OPERATOR = "@rai:allegedly.works"
ROOM = "!room:allegedly.works"

CONFIG = MatrixConfig(homeserver="https://matrix.allegedly.works", user_id=USER, operator_user_id=OPERATOR)


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
    sent: list[tuple[str, str, str]] = field(default_factory=list)
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
        self.sent.append((room_id, body, txn_id))
        return f"$sent-{len(self.sent)}"


def _service(result: SyncResult, store: _FakeStore | None = None) -> tuple[MatrixSyncService, _FakeMatrix, _FakeStore]:
    store = store or _FakeStore()
    service = MatrixSyncService(CONFIG, SecretStr("pw"), engine=None, store=store)  # type: ignore[arg-type]
    matrix = _FakeMatrix(result)
    service._client = matrix  # type: ignore[assignment]
    return service, matrix, store


def _message(body: str, sender: str = OPERATOR, event_id: str = "$evt") -> InboundMessage:
    return InboundMessage(room_id=ROOM, event_id=event_id, sender=sender, body=body, origin_server_ts=1)


async def test_echoes_an_operator_message():
    service, matrix, _ = _service(SyncResult("s2", (_message("hello"),), ()))

    await service.sync_once("tok")

    assert matrix.sent == [(ROOM, "echo: hello", "echo-$evt")]


async def test_joins_an_invite_from_the_operator():
    service, matrix, _ = _service(SyncResult("s2", (), (Invite(room_id=ROOM, inviter=OPERATOR),)))

    await service.sync_once("tok")

    assert matrix.joined == [ROOM]


async def test_leaves_an_invite_from_anybody_else_pending():
    """R3.6 — only the operator's invites are joined; others are surfaced, not acted on."""
    service, matrix, _ = _service(
        SyncResult("s2", (), (Invite(room_id="!other:allegedly.works", inviter="@stranger:allegedly.works"),))
    )

    await service.sync_once("tok")

    assert matrix.joined == []


async def test_watermark_advances_after_the_batch_is_handled():
    service, matrix, store = _service(SyncResult("s2", (_message("hi"),), ()))

    await service.sync_once("tok")

    assert store.saved_batches == ["s2"]
    assert matrix.sent, "the batch must be acted on before its watermark is persisted"


async def test_resumes_from_the_stored_watermark():
    service, matrix, _ = _service(SyncResult("s5", (), ()), store=_FakeStore(next_batch="s4"))

    await service.sync_once("tok")

    assert matrix.since == "s4"


async def test_reuses_a_valid_cached_token():
    """Synapse rate-limits /login, so a working token must not be re-minted (R10.3a)."""
    service, matrix, _ = _service(SyncResult("s2", (), ()), store=_FakeStore(token="cached"))

    assert await service._token() == "cached"
    assert matrix.logins == 0


async def test_logs_in_again_when_the_cached_token_is_rejected():
    service, matrix, store = _service(SyncResult("s2", (), ()), store=_FakeStore(token="stale"))
    matrix.token_valid = False

    assert await service._token() == "fresh-token"
    assert (matrix.logins, store.token) == (1, "fresh-token")


async def test_auth_error_surfaces_so_the_loop_can_re_login():
    """The loop distinguishes a rejected token from a transport failure."""

    async def _reject(token: str, since: str | None) -> SyncResult:
        raise MatrixAuthError("M_UNKNOWN_TOKEN")

    service, matrix, _ = _service(SyncResult("s2", (), ()))
    matrix.sync = _reject  # type: ignore[method-assign]

    try:
        await service.sync_once("tok")
    except MatrixAuthError:
        return
    raise AssertionError("MatrixAuthError should propagate out of sync_once")


if __name__ == "__main__":
    pytest_bazel.main()
