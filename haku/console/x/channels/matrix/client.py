"""Matrix client for the console's chat surface, over `matrix-nio`.

Three deviations from stock nio:

- **Sync position lives in Postgres**, not nio's on-disk store, because the loop can move to a
  different pod (`sync.MatrixSyncStore`). `since` is always passed explicitly and
  `store_sync_tokens` stays off.
- **Failures raise.** nio reports them as result-union values (`SyncError`), and a rejected token
  has to be distinguishable from a transport failure.
- **429s are bounded** (`MAX_RATE_LIMIT_RETRIES`), so `pacer` hears the answer rather than nio
  silently waiting it out.

E2EE is off — the room is a plain DM and encryption is out of scope for this channel (<SPEC.md>) —
so there is no crypto store and no `python-olm`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4, uuid5

from nio import AsyncClient, AsyncClientConfig, ErrorResponse, Response
from nio.api import MessageDirection
from nio.events.invite_events import InviteMemberEvent
from nio.events.misc import BadEvent, BadEventType, UnknownBadEvent
from nio.events.room_events import Event, RoomMessageEmote, RoomMessageFormatted, RoomMessageText
from nio.responses import (
    JoinResponse,
    LoginResponse,
    RoomMessagesResponse,
    RoomRedactResponse,
    RoomSendResponse,
    RoomTypingResponse,
    SyncResponse,
    WhoamiResponse,
)
from pydantic import BaseModel, ConfigDict, Field

from haku.console.x.channels.matrix.formatted_body import to_formatted_body

logger = logging.getLogger(__name__)

# How long the homeserver keeps Haku's typing notice alive without being told again. Short enough
# that a console dying mid-turn leaves an indicator that retires itself.
TYPING_TIMEOUT_MS = 30_000

# Long-poll ceiling. Synapse returns as soon as anything arrives, so this only bounds how
# long a quiet connection stays open before it is re-established.
SYNC_TIMEOUT_MS = 30_000

# Events per room per `/sync`, and per backfill page. Above this a room's timeline comes back
# truncated and the gap has to be paginated (`_backfill`).
TIMELINE_LIMIT = 100

# Ceiling on backfill pagination for one room in one pass, so a room that was busy for a week
# cannot stall the loop. Hitting it loses messages, so it is logged.
MAX_BACKFILL_PAGES = 20

_STEADY_FILTER = {"room": {"timeline": {"limit": TIMELINE_LIMIT}}}

# The first sync has no watermark, so there is no missed range to replay — only a position to
# establish. Pulling backlog here would answer messages that predate the console. Invites arrive
# in `invite_state` rather than the timeline, so they still come through.
_INITIAL_FILTER = {"room": {"timeline": {"limit": 0}}}

# Errcodes that mean "this token is no longer good", as opposed to a transport failure.
_AUTH_ERRCODES = frozenset({"M_UNKNOWN_TOKEN", "M_MISSING_TOKEN"})

# How many times nio may absorb a 429 inside one request before the error reaches us.
#
# **Gotcha: nio's default is unlimited, not off** — a rate-limited send stopped returning rather
# than erroring (<../../../docs/chat_runtime_facts.md>). Two retries keep a single burst invisible
# while letting a sustained one reach `pacer`, the only place the room's real budget is learned.
MAX_RATE_LIMIT_RETRIES = 2

# Stable, private namespace for Matrix transactions derived from durable conversation events.
# The resulting transaction id reveals neither the conversation UUID nor the event position, while
# replaying one projection reaches the same homeserver transaction cache entry.
_PROJECTED_NOTICE_NAMESPACE = UUID("2a56af77-06d8-4d08-a360-ab8354170eab")


# The console's own key inside an event's `content`. Reverse-DNS on the deployment's domain,
# which is the Matrix convention for a field outside the spec's namespace.
HAKU_CONTENT_KEY = "works.allegedly.haku"

# The msgtype for "an automated client is talking". Everything Haku says that is not an answer
# goes out under it, and nothing that arrives under it is ever read as input — see `_read`.
NOTICE_MSGTYPE = "m.notice"


class RoomEventKind(StrEnum):
    """What the console meant by an event it sent, in place of inferring it from the msgtype."""

    REPLY = "reply"
    STATUS = "status"
    NARRATION = "narration"
    LIFECYCLE = "lifecycle"
    REJECTED = "rejected"
    ROOM = "room"
    UNREADABLE = "unreadable"


class ConversationEventSource(BaseModel):
    """The durable conversation fact from which a Matrix event was projected."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    attachment_id: UUID
    conversation_id: UUID
    event_seq: int = Field(gt=0)


class EventTag(BaseModel):
    """What the console states about an event it is sending.

    **Write-only.** Nothing reads a tag back off an event — ingress excludes Haku's own sender, and
    re-awakening reads the console's transcript — so the reader it is for is in the room.

    **Ids and kinds only.** The room is public and federated, so a tag carrying text would publish
    the same thing twice, in a field nobody renders.

    **Conversation ids, never a session's.** A room event is permanent and federated, so an id in
    its tag outlives every session it could name — and the thread a room holds a copy of is the
    conversation (<../../../docs/chat_layers.md>).
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    kind: RoomEventKind
    conversation_id: UUID | None = None
    source: ConversationEventSource | None = None

    def content(self) -> dict[str, Any]:
        """The tag as it goes on the wire, with an absent field absent rather than null."""
        return self.model_dump(mode="json", exclude_none=True)

    def transaction_id(self) -> str:
        """What to send this event under.

        A notice projected from a durable conversation row carries its attachment and source fields and gets a
        deterministic transaction id. Re-reading that row before its cursor advances therefore
        asks Synapse for the same send instead of posting a second event. This is bounded by the
        homeserver's transaction-cache lifetime; it is replay protection, not durable exactly-once
        correspondence.

        Everything else tagged this way — a status edit, room binding, or supervisor notice — has
        no durable source to name and deliberately mints a fresh id. A reply uses its outbox row's
        id instead (`outbox.PendingReply.transaction_id`).

        Rests on how Synapse keys and expires its transaction cache
        (<../../../docs/chat_runtime_facts.md>).
        """
        if self.source is not None:
            return uuid5(
                _PROJECTED_NOTICE_NAMESPACE,
                f"{self.source.attachment_id}:{self.source.conversation_id}:{self.source.event_seq}",
            ).hex
        return uuid4().hex


class MatrixError(Exception):
    """The homeserver returned an error for a call we made.

    `retry_after_ms` is a 429's own answer to "how long", carried rather than formatted into
    the message because `pacer` acts on it: it is the only measurement of the room's
    real budget that this console ever receives.
    """

    def __init__(self, message: str, *, retry_after_ms: int | None = None):
        super().__init__(message)
        self.retry_after_ms = retry_after_ms


class MatrixAuthError(MatrixError):
    """The homeserver rejected our access token.

    Distinct so the caller can re-login rather than back off: Synapse invalidates tokens on
    password set and on restore from an older backup.
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
class UnmappableEvent:
    """An `m.room.message` this console has no way to read as prose.

    A screenshot, a voice memo, a file, plus any msgtype invented after this release. Carried out
    of the sync rather than filtered away because the operator has to be told about it; `sync` is
    what tells them.

    No body: a media event's `body` is a filename, and repeating it back would read as though the
    thing had been understood.
    """

    room_id: str
    event_id: str
    sender: str
    msgtype: str


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
    unmappable: tuple[UnmappableEvent, ...] = ()


def _msgtype(event: Event | BadEvent) -> str | None:
    """The `msgtype` of an `m.room.message`, or None for anything else in a timeline.

    Read off the raw source rather than nio's parsed class, because the events this exists to catch
    are the ones nio models least: an msgtype it does not know becomes `RoomMessageUnknown` with no
    shared base to check, and content that fails its schema becomes a `BadEvent` with no msgtype
    attribute — while both carry it in `source`.

    None means "not a message": a state event, a non-message event such as a reaction, and a
    redaction, which keeps its type and loses its content.
    """
    match event.source:
        case {"type": "m.room.message", "content": {"msgtype": str(msgtype)}}:
            return msgtype
        case _:
            return None


def _unwrap[R: Response](response: Response, expected: type[R]) -> R:
    if isinstance(response, expected):
        return response
    if isinstance(response, ErrorResponse):
        error = MatrixAuthError if response.status_code in _AUTH_ERRCODES else MatrixError
        raise error(f"{response.status_code}: {response.message}", retry_after_ms=response.retry_after_ms)
    raise MatrixError(f"unexpected {type(response).__name__} where {expected.__name__} was required")


class MatrixClient:
    """The client-API calls the sync loop makes, as one authenticated identity."""

    def __init__(self, homeserver: str, user_id: str, device_id: str):
        self._user_id = user_id
        self._client = AsyncClient(
            homeserver=homeserver.rstrip("/"),
            user=user_id,
            device_id=device_id,
            config=AsyncClientConfig(
                encryption_enabled=False,
                request_timeout=SYNC_TIMEOUT_MS / 1000 + 15,
                max_limit_exceeded=MAX_RATE_LIMIT_RETRIES,
            ),
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
        messages, unmappable = await self._timelines(response, since)
        return SyncResult(
            next_batch=response.next_batch, messages=messages, invites=self._invites(response), unmappable=unmappable
        )

    async def join(self, token: str, room_id: str) -> None:
        self._client.access_token = token
        _unwrap(await self._client.join(room_id), JoinResponse)

    async def send_text(self, token: str, room_id: str, body: str, txn_id: str, tag: EventTag) -> str:
        """Send Haku's reply, rendering its Markdown for clients that display HTML.

        `body` stays the Markdown source: it is the spec's fallback for clients that show no
        formatting. `txn_id` makes the send idempotent — a retry with the same value is
        deduplicated by the homeserver; `EventTag.transaction_id` is where a caller gets one.
        """
        return await self._send(token, room_id, "m.text", body, txn_id, tag, formatted=to_formatted_body(body))

    async def send_notice(self, token: str, room_id: str, body: str, txn_id: str, tag: EventTag) -> str:
        """Send an `m.notice`, returning its event ID.

        Lifecycle and status messages are notices rather than plain text so clients style them
        apart from Haku's answers and bots ignore them by convention.
        """
        return await self._send(token, room_id, NOTICE_MSGTYPE, body, txn_id, tag)

    async def edit_notice(self, token: str, room_id: str, event_id: str, body: str, txn_id: str, tag: EventTag) -> None:
        """Replace an earlier notice in place, rather than posting a second one.

        This is what lets a turn have **one** status line instead of a line per step. The edit is
        its own event carrying `m.replace`: clients that understand it re-render the original, and
        clients that do not show the fallback body — which is why the top-level `body` is the new
        text prefixed with `*`, per the spec's convention.
        """
        self._client.access_token = token
        # The tag rides on both halves: `m.new_content` is what a client that understands the
        # edit renders, and the outer content is what one that does not falls back to reading.
        new_content: dict[str, Any] = {"msgtype": NOTICE_MSGTYPE, "body": body, HAKU_CONTENT_KEY: tag.content()}
        _unwrap(
            await self._client.room_send(
                room_id,
                message_type="m.room.message",
                content=new_content
                | {
                    "body": f"* {body}",
                    "m.new_content": new_content,
                    "m.relates_to": {"rel_type": "m.replace", "event_id": event_id},
                },
                tx_id=txn_id,
            ),
            RoomSendResponse,
        )

    async def set_typing(self, token: str, room_id: str, *, active: bool) -> None:
        """Start or stop Haku's typing notification in *room_id*.

        The homeserver expires the notice by itself after `TYPING_TIMEOUT_MS`, so a console that
        dies mid-turn leaves an indicator that goes away on its own. The cost is that a live turn
        has to say it again before that expiry, which the turn's status driver does.
        """
        self._client.access_token = token
        _unwrap(await self._client.room_typing(room_id, active, timeout=TYPING_TIMEOUT_MS), RoomTypingResponse)

    async def redact(self, token: str, room_id: str, event_id: str, reason: str) -> None:
        """Remove an event. Used to retire a status line once its answer has posted.

        A redaction rather than a final edit: once the answer is in the room the line is spent, and
        one edited to "done" on every turn is the clutter the single status line exists to avoid.
        """
        self._client.access_token = token
        _unwrap(await self._client.room_redact(room_id, event_id, reason=reason), RoomRedactResponse)

    async def _send(
        self,
        token: str,
        room_id: str,
        msgtype: str,
        body: str,
        txn_id: str,
        tag: EventTag,
        formatted: str | None = None,
    ) -> str:
        self._client.access_token = token
        content: dict[str, Any] = {"msgtype": msgtype, "body": body, HAKU_CONTENT_KEY: tag.content()}
        if formatted is not None:
            content |= {"format": "org.matrix.custom.html", "formatted_body": formatted}
        response = _unwrap(
            await self._client.room_send(room_id, message_type="m.room.message", content=content, tx_id=txn_id),
            RoomSendResponse,
        )
        return response.event_id

    def _read(
        self, room_id: str, events: Iterable[Event | BadEventType]
    ) -> tuple[list[InboundMessage], list[UnmappableEvent]]:
        """Split a room's timeline into what Haku can read and what it can only report.

        - **Our own events are dropped first**, before anything is classified, which is what keeps
          a notice about an unreadable event from itself being an unreadable event.
        - **`m.emote` is prose and is serviced.** It is `m.text` in the third person, with the
          words in `body`.
        - **`m.notice` is ignored rather than reported.** It is the msgtype for automated clients,
          which is what Haku's own status, lifecycle and unreadable-event lines go out under. The
          sender rule above excludes ours; this excludes anything else's, so no notice in this room
          can produce a notice about it — the second of two independent guards against a
          self-feeding loop.

        Everything else that is an `m.room.message` is unmappable: the operator gets told rather
        than nothing happening.
        """
        messages: list[InboundMessage] = []
        unmappable: list[UnmappableEvent] = []
        for event in events:
            if isinstance(event, UnknownBadEvent):
                # nio found no event id or no sender, so there is nothing to service and nothing
                # to attribute. It logs the source itself when it gives up on one.
                continue
            if event.sender == self._user_id:
                continue
            if (msgtype := _msgtype(event)) is None:
                continue
            if isinstance(event, RoomMessageText | RoomMessageEmote):
                messages.append(self._inbound(room_id, event))
            elif msgtype == NOTICE_MSGTYPE:
                logger.info("Matrix: ignoring notice %s from %s", event.event_id, event.sender)
            else:
                logger.warning(
                    "Matrix: %s from %s is %s, which Haku cannot read", event.event_id, event.sender, msgtype
                )
                unmappable.append(
                    UnmappableEvent(room_id=room_id, event_id=event.event_id, sender=event.sender, msgtype=msgtype)
                )
        return messages, unmappable

    async def _timelines(
        self, response: SyncResponse, since: str | None
    ) -> tuple[tuple[InboundMessage, ...], tuple[UnmappableEvent, ...]]:
        messages: list[InboundMessage] = []
        unmappable: list[UnmappableEvent] = []
        for room_id, room in response.rooms.join.items():
            # A truncated timeline is a gap that would otherwise silently swallow what arrived
            # while the console was down. On the first sync there is no range to recover.
            if room.timeline.limited and since is not None and room.timeline.prev_batch is not None:
                recovered, unreadable = await self._backfill(room_id, room.timeline.prev_batch, since)
                messages.extend(recovered)
                unmappable.extend(unreadable)
            live, unreadable = self._read(room_id, room.timeline.events)
            messages.extend(live)
            unmappable.extend(unreadable)
        return tuple(messages), tuple(unmappable)

    async def _backfill(
        self, room_id: str, prev_batch: str, since: str
    ) -> tuple[list[InboundMessage], list[UnmappableEvent]]:
        """What arrived between `since` and the start of a truncated timeline, oldest first."""
        logger.warning("Matrix: %s timeline truncated, backfilling from %s", room_id, since)
        recovered: list[InboundMessage] = []
        unmappable: list[UnmappableEvent] = []
        start = prev_batch
        for _ in range(MAX_BACKFILL_PAGES):
            page = _unwrap(
                await self._client.room_messages(
                    room_id, start=start, end=since, direction=MessageDirection.back, limit=TIMELINE_LIMIT
                ),
                RoomMessagesResponse,
            )
            messages, unreadable = self._read(room_id, page.chunk)
            recovered.extend(messages)
            unmappable.extend(unreadable)
            if not page.chunk or page.end is None:
                # Reached the watermark: `/messages` stops at `end` and returns nothing past it.
                break
            start = page.end
        else:
            logger.error(
                "Matrix: gave up backfilling %s after %d pages — messages between %s and %s are lost",
                room_id,
                MAX_BACKFILL_PAGES,
                since,
                prev_batch,
            )
        recovered.reverse()
        unmappable.reverse()
        return recovered, unmappable

    def _invites(self, response: SyncResponse) -> tuple[Invite, ...]:
        return tuple(
            Invite(room_id=room_id, inviter=event.sender)
            for room_id, room in response.rooms.invite.items()
            for event in room.invite_state
            if isinstance(event, InviteMemberEvent)
            and event.state_key == self._user_id
            and event.membership == "invite"
        )

    def _inbound(self, room_id: str, event: RoomMessageFormatted) -> InboundMessage:
        return InboundMessage(
            room_id=room_id,
            event_id=event.event_id,
            sender=event.sender,
            body=event.body,
            origin_server_ts=event.server_timestamp,
        )
