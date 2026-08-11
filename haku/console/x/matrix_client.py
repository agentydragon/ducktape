"""Matrix client for the console's chat surface, over `matrix-nio`.

Two deviations from stock nio, both forced by the console being a leader-elected replica
set rather than a single long-lived process:

- **Sync position lives in Postgres**, not nio's on-disk store, because the loop can move
  to a different pod (`matrix_sync.MatrixSyncStore`). `since` is always passed explicitly
  and `store_sync_tokens` stays off.
- **Failures raise.** nio reports them as result-union values (`SyncError`); the loop needs
  a rejected token to be distinguishable from a transport failure, so every call here
  converts errors to exceptions.

E2EE is off (`haku/plans/matrix_chat_runtime.md` — the room is a plain DM), so no crypto
store, no `python-olm`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from nio import AsyncClient, AsyncClientConfig, ErrorResponse, Response
from nio.api import MessageDirection
from nio.events.invite_events import InviteMemberEvent
from nio.events.room_events import RoomMessageText
from nio.responses import (
    JoinResponse,
    LoginResponse,
    RoomMessagesResponse,
    RoomSendResponse,
    SyncResponse,
    WhoamiResponse,
)

logger = logging.getLogger(__name__)

# Long-poll ceiling. Synapse returns as soon as anything arrives, so this only bounds how
# long a quiet connection stays open before it is re-established.
SYNC_TIMEOUT_MS = 30_000

# Events per room per `/sync`, and per backfill page. Above this a room's timeline comes
# back truncated and the gap has to be paginated — see `_backfill`. Raising it makes that
# rare rather than making it impossible.
TIMELINE_LIMIT = 100

# Ceiling on backfill pagination for one room in one pass, so a room that was busy for a
# week cannot stall the loop indefinitely. Hitting it loses messages, so it is logged.
MAX_BACKFILL_PAGES = 20

_STEADY_FILTER = {"room": {"timeline": {"limit": TIMELINE_LIMIT}}}

# The first sync has no watermark, so there is no missed range to replay — only a position
# to establish. Pulling backlog here would make the console answer messages that predate
# it. Invites arrive in `invite_state` rather than the timeline, so they still come through.
_INITIAL_FILTER = {"room": {"timeline": {"limit": 0}}}

# Errcodes that mean "this token is no longer good", as opposed to a transport failure.
_AUTH_ERRCODES = frozenset({"M_UNKNOWN_TOKEN", "M_MISSING_TOKEN"})


class MatrixError(Exception):
    """The homeserver returned an error for a call we made."""


class MatrixAuthError(MatrixError):
    """The homeserver rejected our access token.

    Distinct so the caller can re-login rather than back off: Synapse invalidates tokens on
    password set and on restore from an older backup, and the console is expected to
    recover by logging in again (R10.3a).
    """


@dataclass(frozen=True)
class InboundMessage:
    """One `m.room.message` addressed to us."""

    room_id: str
    event_id: str
    sender: str
    body: str
    origin_server_ts: int


@dataclass(frozen=True)
class Invite:
    """A pending room invitation and who issued it."""

    room_id: str
    inviter: str


@dataclass(frozen=True)
class SyncResult:
    next_batch: str
    messages: tuple[InboundMessage, ...]
    invites: tuple[Invite, ...]


def _unwrap[R: Response](response: Response, expected: type[R]) -> R:
    if isinstance(response, expected):
        return response
    if isinstance(response, ErrorResponse):
        error = MatrixAuthError if response.status_code in _AUTH_ERRCODES else MatrixError
        raise error(f"{response.status_code}: {response.message}")
    raise MatrixError(f"unexpected {type(response).__name__} where {expected.__name__} was required")


class MatrixClient:
    """The client-API calls the sync loop makes, as one authenticated identity."""

    def __init__(self, homeserver: str, user_id: str, device_id: str):
        self._user_id = user_id
        self._client = AsyncClient(
            homeserver=homeserver.rstrip("/"),
            user=user_id,
            device_id=device_id,
            config=AsyncClientConfig(encryption_enabled=False, request_timeout=SYNC_TIMEOUT_MS / 1000 + 15),
        )

    async def close(self) -> None:
        await self._client.close()

    async def login(self, password: str) -> str:
        """Password login, returning an access token.

        The device ID is the one pinned at construction, so repeated logins reuse one device
        instead of leaving a new one behind on every restart.
        """
        return _unwrap(await self._client.login(password), LoginResponse).access_token

    async def whoami(self, token: str) -> bool:
        """True if `token` still authenticates as us."""
        self._client.access_token = token
        response = await self._client.whoami()
        return isinstance(response, WhoamiResponse) and response.user_id == self._user_id

    async def sync(self, token: str, since: str | None) -> SyncResult:
        """One long-poll `/sync`, parsed down to what the loop acts on."""
        self._client.access_token = token
        response = _unwrap(
            await self._client.sync(
                timeout=SYNC_TIMEOUT_MS,
                since=since,
                sync_filter=_STEADY_FILTER if since is not None else _INITIAL_FILTER,
            ),
            SyncResponse,
        )
        return SyncResult(
            next_batch=response.next_batch,
            messages=await self._messages(response, since),
            invites=self._invites(response),
        )

    async def join(self, token: str, room_id: str) -> None:
        self._client.access_token = token
        _unwrap(await self._client.join(room_id), JoinResponse)

    async def recent_messages(self, token: str, room_id: str, since: str, limit: int) -> tuple[InboundMessage, ...]:
        """The last `limit` conversational messages before `since`, oldest first.

        **Filters the opposite way from `sync`.** Ingress drops Haku's own messages, because
        answering yourself is a loop (R1.5); history must keep them, because half a
        conversation is not context (R3.3a). Same room, same events, so the two read paths
        cannot share a filter.

        Lifecycle notices are excluded for free: the console sends them as `m.notice`, which
        nio parses as `RoomMessageNotice`, a different class from the `RoomMessageText` this
        keeps. Re-awakening a session with its own status chatter would be noise.

        `since` is a `/sync` watermark, which is also a valid `/messages` pagination token —
        so this reads back from wherever the loop has got to, with no second position to keep.
        """
        self._client.access_token = token
        page = _unwrap(
            await self._client.room_messages(room_id, start=since, direction=MessageDirection.back, limit=limit),
            RoomMessagesResponse,
        )
        recent = [self._inbound(room_id, event) for event in page.chunk if isinstance(event, RoomMessageText)]
        recent.reverse()
        return tuple(recent)

    async def send_text(self, token: str, room_id: str, body: str, txn_id: str) -> str:
        """Send a plain-text message, returning its event ID.

        `txn_id` makes the send idempotent: a retry with the same value is deduplicated by
        the homeserver rather than posting twice.
        """
        return await self._send(token, room_id, "m.text", body, txn_id)

    async def send_notice(self, token: str, room_id: str, body: str, txn_id: str) -> str:
        """Send an `m.notice`, returning its event ID.

        Lifecycle and status messages are notices rather than plain text (R7) so clients
        style them apart from Haku's answers and bots ignore them by convention.
        """
        return await self._send(token, room_id, "m.notice", body, txn_id)

    async def _send(self, token: str, room_id: str, msgtype: str, body: str, txn_id: str) -> str:
        self._client.access_token = token
        response = _unwrap(
            await self._client.room_send(
                room_id, message_type="m.room.message", content={"msgtype": msgtype, "body": body}, tx_id=txn_id
            ),
            RoomSendResponse,
        )
        return response.event_id

    async def _messages(self, response: SyncResponse, since: str | None) -> tuple[InboundMessage, ...]:
        messages: list[InboundMessage] = []
        for room_id, room in response.rooms.join.items():
            # A truncated timeline is the gap that would otherwise silently swallow whatever
            # arrived while the console was down — the very thing the watermark exists to
            # prevent (R1.7). On the first sync there is no range to recover, only a
            # position to take.
            if room.timeline.limited and since is not None and room.timeline.prev_batch is not None:
                messages.extend(await self._backfill(room_id, room.timeline.prev_batch, since))
            messages.extend(
                self._inbound(room_id, event) for event in room.timeline.events if isinstance(event, RoomMessageText)
            )
        # Our own posts come back through /sync and are never input (R1.5).
        return tuple(message for message in messages if message.sender != self._user_id)

    async def _backfill(self, room_id: str, prev_batch: str, since: str) -> list[InboundMessage]:
        """Messages between `since` and the start of a truncated timeline, oldest first."""
        logger.warning("Matrix: %s timeline truncated, backfilling from %s", room_id, since)
        recovered: list[InboundMessage] = []
        start = prev_batch
        for _ in range(MAX_BACKFILL_PAGES):
            page = _unwrap(
                await self._client.room_messages(
                    room_id, start=start, end=since, direction=MessageDirection.back, limit=TIMELINE_LIMIT
                ),
                RoomMessagesResponse,
            )
            recovered.extend(
                self._inbound(room_id, event) for event in page.chunk if isinstance(event, RoomMessageText)
            )
            if not page.chunk or page.end is None:
                # Reached the watermark: `/messages` stops at `end` and returns nothing past it.
                recovered.reverse()
                return recovered
            start = page.end
        logger.error(
            "Matrix: gave up backfilling %s after %d pages — messages between %s and %s are lost",
            room_id,
            MAX_BACKFILL_PAGES,
            since,
            prev_batch,
        )
        recovered.reverse()
        return recovered

    def _invites(self, response: SyncResponse) -> tuple[Invite, ...]:
        return tuple(
            Invite(room_id=room_id, inviter=event.sender)
            for room_id, room in response.rooms.invite.items()
            for event in room.invite_state
            if isinstance(event, InviteMemberEvent)
            and event.state_key == self._user_id
            and event.membership == "invite"
        )

    def _inbound(self, room_id: str, event: RoomMessageText) -> InboundMessage:
        return InboundMessage(
            room_id=room_id,
            event_id=event.event_id,
            sender=event.sender,
            body=event.body,
            origin_server_ts=event.server_timestamp,
        )
