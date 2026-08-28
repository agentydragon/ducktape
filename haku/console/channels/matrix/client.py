"""Matrix client for the console's chat surface, over `matrix-nio`.

Three deviations from stock nio:

- **Sync position lives in Postgres**, not nio's on-disk store, because the loop can move to a
  different pod (`sync.SyncStore`). `since` is always passed explicitly and
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
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
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
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from haku.console.channels.matrix.formatted_body import to_formatted_body

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
# than erroring (<../../docs/chat_runtime_facts.md>). Two retries keep a single burst invisible
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

    **Read back by the correspondence reader.** Ingress excludes Haku's own sender before anything
    is classified; `_own_copy` is the mirror — only our sender, parse the tag, never a prompt — and
    `room_copy` durably keeps the `source` it reads, which is how a restarted reconciler finds the
    event already showing a conversation fact instead of posting it again.

    **Ids and kinds only.** The room is public and federated, so a tag carrying text would publish
    the same thing twice, in a field nobody renders.

    **Conversation ids, never a session's.** A room event is permanent and federated, so an id in
    its tag outlives every session it could name — and the thread a room holds a copy of is the
    conversation (<../../docs/chat_layers.md>).
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

        An event that carries a `source` — a sealed notice, or a span line's create — gets a
        deterministic transaction id. Re-deriving that send before its cursor or revision row
        committed therefore asks Synapse for the same send instead of posting a second event. The
        homeserver's transaction cache bounds this to 30-to-60 minutes, which is only the window
        between a successful send and its `/sync` echo reaching `room_copy` — past the echo, the
        reconciler finds the durable correspondence and does not send at all.

        A span line's *edits* pass an explicit fresh id instead — each edit is its own event, and a
        lost one is recomputed rather than replayed. A tag with no source — a room-binding notice —
        mints a fresh id here, and a reply uses its outbox row's id
        (`outbox.PendingReply.transaction_id`).

        Rests on how Synapse keys and expires its transaction cache
        (<../../docs/chat_runtime_facts.md>).
        """
        if self.source is not None:
            return uuid5(
                _PROJECTED_NOTICE_NAMESPACE,
                f"{self.source.attachment_id}:{self.source.conversation_id}:{self.source.event_seq}",
            ).hex
        return uuid4().hex


class Error(Exception):
    """The homeserver returned an error for a call we made.

    `retry_after_ms` is a 429's own answer to "how long", carried rather than formatted into
    the message because `pacer` acts on it: it is the only measurement of the room's
    real budget that this console ever receives.
    """

    def __init__(self, message: str, *, retry_after_ms: int | None = None):
        super().__init__(message)
        self.retry_after_ms = retry_after_ms


class AuthError(Error):
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
class ProjectedEvent:
    """One event of Haku's own whose tag names the conversation event it projects.

    What the correspondence reader records (`room_copy`): the room's copy of a sealed notice, read
    back off the same `/sync` ingress uses, under the opposite sender filter.
    """

    room_id: str
    event_id: str
    source: ConversationEventSource
    origin_server_ts: int
    # The event this one revises (`m.replace`), absent on an original post. An edit changes what an
    # event the room already shows says; it is never a second copy of the source.
    replaces_event_id: str | None


@dataclass(frozen=True)
class Redaction:
    """An event the room stopped showing (`m.room.redaction`), whoever unsaid it.

    Not filtered to Haku's sender: the operator can redact Haku's copy too, and whether the target
    was ours is the store's to know.
    """

    room_id: str
    redacts_event_id: str


@dataclass(frozen=True)
class SyncResult:
    next_batch: str
    messages: tuple[InboundMessage, ...]
    invites: tuple[Invite, ...]
    unmappable: tuple[UnmappableEvent, ...] = ()
    projected: tuple[ProjectedEvent, ...] = ()
    redactions: tuple[Redaction, ...] = ()


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


@dataclass
class _Timeline:
    """One sync's timeline events, split by reader.

    `_read` and `_own_copy` are the two readers of one `/sync` with opposite sender filters; this
    is where their outputs travel together without either seeing the other's.
    """

    messages: list[InboundMessage] = field(default_factory=list)
    unmappable: list[UnmappableEvent] = field(default_factory=list)
    projected: list[ProjectedEvent] = field(default_factory=list)
    redactions: list[Redaction] = field(default_factory=list)

    def extend(self, other: _Timeline) -> None:
        self.messages.extend(other.messages)
        self.unmappable.extend(other.unmappable)
        self.projected.extend(other.projected)
        self.redactions.extend(other.redactions)

    def reverse(self) -> None:
        self.messages.reverse()
        self.unmappable.reverse()
        self.projected.reverse()
        self.redactions.reverse()


def _replaces(content: dict[str, Any]) -> str | None:
    """The event an `m.replace` edit revises, or None for an original post."""
    match content.get("m.relates_to"):
        case {"rel_type": "m.replace", "event_id": str(event_id)}:
            return event_id
        case _:
            return None


def _unwrap[R: Response](response: Response, expected: type[R]) -> R:
    if isinstance(response, expected):
        return response
    if isinstance(response, ErrorResponse):
        error = AuthError if response.status_code in _AUTH_ERRCODES else Error
        raise error(f"{response.status_code}: {response.message}", retry_after_ms=response.retry_after_ms)
    raise Error(f"unexpected {type(response).__name__} where {expected.__name__} was required")


class Client:
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
        timeline = await self._timelines(response, since)
        return SyncResult(
            next_batch=response.next_batch,
            messages=tuple(timeline.messages),
            invites=self._invites(response),
            unmappable=tuple(timeline.unmappable),
            projected=tuple(timeline.projected),
            redactions=tuple(timeline.redactions),
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

    def _own_copy(
        self, room_id: str, events: Sequence[Event | BadEventType]
    ) -> tuple[list[ProjectedEvent], list[Redaction]]:
        """The mirror of `_read`: what the room shows of the console's own projected sends.

        Only our sender, parse the tag, never a prompt — the opposite filter on the same `/sync`,
        so nothing read here can become input (`_read` keeps dropping our events before ingress
        classifies anything). Redactions are the exception to the sender rule: whoever unsaid an
        event, what matters is that the room stopped showing it.

        A tag without a `source` — a reply, the status line, a room notice — names no durable
        conversation event, so there is no correspondence to read off it.
        """
        projected: list[ProjectedEvent] = []
        redactions: list[Redaction] = []
        for event in events:
            if isinstance(event, UnknownBadEvent):
                continue
            content = event.source.get("content")
            if event.source.get("type") == "m.room.redaction":
                # `redacts` is top-level, and additionally inside `content` from room v11 on;
                # either spelling names the target.
                target = event.source.get("redacts") or (content.get("redacts") if isinstance(content, dict) else None)
                if isinstance(target, str):
                    redactions.append(Redaction(room_id=room_id, redacts_event_id=target))
                continue
            if event.sender != self._user_id or event.source.get("type") != "m.room.message":
                continue
            if not isinstance(content, dict) or not isinstance(tag := content.get(HAKU_CONTENT_KEY), dict):
                continue
            if not isinstance(raw_source := tag.get("source"), dict):
                continue
            try:
                source = ConversationEventSource.model_validate(raw_source)
            except ValidationError:
                # Our own writer — possibly a newer release — so unreadable degrades to missing
                # correspondence for this one event rather than a wedged sync loop.
                logger.warning("Matrix: cannot read the source on our own event %s", event.event_id, exc_info=True)
                continue
            projected.append(
                ProjectedEvent(
                    room_id=room_id,
                    event_id=event.event_id,
                    source=source,
                    origin_server_ts=event.server_timestamp,
                    replaces_event_id=_replaces(content),
                )
            )
        return projected, redactions

    def _collect(self, room_id: str, events: Sequence[Event | BadEventType], into: _Timeline) -> None:
        messages, unmappable = self._read(room_id, events)
        into.messages.extend(messages)
        into.unmappable.extend(unmappable)
        projected, redactions = self._own_copy(room_id, events)
        into.projected.extend(projected)
        into.redactions.extend(redactions)

    async def _timelines(self, response: SyncResponse, since: str | None) -> _Timeline:
        timeline = _Timeline()
        for room_id, room in response.rooms.join.items():
            # A truncated timeline is a gap that would otherwise silently swallow what arrived
            # while the console was down. On the first sync there is no range to recover.
            if room.timeline.limited and since is not None and room.timeline.prev_batch is not None:
                timeline.extend(await self._backfill(room_id, room.timeline.prev_batch, since))
            self._collect(room_id, room.timeline.events, timeline)
        return timeline

    async def _backfill(self, room_id: str, prev_batch: str, since: str) -> _Timeline:
        """What arrived between `since` and the start of a truncated timeline, oldest first."""
        logger.warning("Matrix: %s timeline truncated, backfilling from %s", room_id, since)
        recovered = _Timeline()
        start = prev_batch
        for _ in range(MAX_BACKFILL_PAGES):
            page = _unwrap(
                await self._client.room_messages(
                    room_id, start=start, end=since, direction=MessageDirection.back, limit=TIMELINE_LIMIT
                ),
                RoomMessagesResponse,
            )
            self._collect(room_id, page.chunk, recovered)
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

    def _inbound(self, room_id: str, event: RoomMessageFormatted) -> InboundMessage:
        return InboundMessage(
            room_id=room_id,
            event_id=event.event_id,
            sender=event.sender,
            body=event.body,
            origin_server_ts=event.server_timestamp,
        )
