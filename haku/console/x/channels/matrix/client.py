"""Matrix client for the console's chat surface, over `matrix-nio`.

Three deviations from stock nio:

- **Sync position lives in Postgres**, not nio's on-disk store, because the loop can move
  to a different pod (`sync.MatrixSyncStore`). `since` is always passed explicitly
  and `store_sync_tokens` stays off.
- **Failures raise.** nio reports them as result-union values (`SyncError`); the loop needs
  a rejected token to be distinguishable from a transport failure, so every call here
  converts errors to exceptions.
- **429s are bounded** (`MAX_RATE_LIMIT_RETRIES`), so being rate-limited is something the
  console finds out about rather than something it silently waits out.

The first two are forced by the console being a leader-elected replica set rather than a
single long-lived process; the third by `pacer` needing to hear the answer.

E2EE is off (`haku/plans/matrix_chat_runtime.md` — the room is a plain DM), so no crypto
store, no `python-olm`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

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
from pydantic import BaseModel, ConfigDict

from haku.console.x.channels.matrix.formatted_body import to_formatted_body

logger = logging.getLogger(__name__)

# How long the homeserver keeps Haku's typing notice alive without being told again (R6.1). Short
# enough that a console dying mid-turn leaves an indicator that retires itself, long enough that
# the turn's status driver refreshes it a handful of times rather than constantly.
TYPING_TIMEOUT_MS = 30_000

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

# How many times nio may absorb a 429 inside one request before the error reaches us.
#
# **Gotcha: nio's default is unlimited, not off** — a rate-limited send never returned an error,
# it stopped returning (<../../../docs/chat_runtime_facts.md>). Two retries keep a single
# burst invisible while letting a sustained one reach `pacer`, which is the only place the
# room's real budget can be learned.
MAX_RATE_LIMIT_RETRIES = 2


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


class EventTag(BaseModel):
    """What the console states about an event it is sending.

    **Write-only, for now.** Nothing here reads a tag back off an event: ingress excludes Haku's
    own sender (R1.5) and re-awakening reads the console's transcript rather than the room, so
    what the tag is for is a reader in the room — Element showing which session answered, and the
    room-read tools of R11.3 when they land, which is what brings a parser back with it.

    **Ids and kinds only.** This room is public and federated, so the content travels to every
    server in it and is not ours to take back; a tag that carried text would be publishing the
    same thing twice, in a field nobody renders. Redaction strips it along with the rest of
    `content`, which is the behaviour a status line's retirement wants anyway.

    `message_id` is the transcript row and `agent_message_id` is the agent's own `msg_…`. Both are
    absent on everything that is not a reply, because nothing else corresponds to a row.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    kind: RoomEventKind
    session_id: UUID | None = None
    message_id: UUID | None = None
    agent_message_id: str | None = None

    def content(self) -> dict[str, Any]:
        """The tag as it goes on the wire: `mode="json"` for the UUIDs, `exclude_none` so an
        absent field is absent rather than null."""
        return self.model_dump(mode="json", exclude_none=True)

    def transaction_id(self) -> str:
        """What to send this event under, so a re-send of the same thing is not a second event.

        **Derived where the event names a transcript row, fresh where it does not.** Re-posting a
        row is always a mistake, so its id is the transaction and the homeserver refuses the
        duplicate; a status edit and a lifecycle notice name no row, and deriving one would be a
        way to lose the event rather than to deduplicate it.

        Second line of defence, not first — `frame_uid` drops a replayed frame before any send —
        and it rests on how Synapse keys and expires its transaction cache
        (<../../../docs/chat_runtime_facts.md>). Impure, and called once per send.
        """
        return self.message_id.hex if self.message_id is not None else uuid4().hex


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
    password set and on restore from an older backup, and the console is expected to
    recover by logging in again (R10.3a).
    """


@dataclass(frozen=True)
class InboundMessage:
    """One `m.room.message` addressed to us.

    It carries no parsed `EventTag`, and cannot usefully: `_read` drops everything Haku sent
    (R1.5) and nothing else in the room tags anything, so the field was structurally always
    absent once the history read — its only consumer — stopped going to the homeserver
    (`sync.recent_history`).
    """

    room_id: str
    event_id: str
    sender: str
    body: str
    origin_server_ts: int


@dataclass(frozen=True)
class UnmappableEvent:
    """An `m.room.message` this console has no way to read as prose.

    A screenshot, a voice memo, a file — anything whose meaning is in an attachment rather than
    in `body`, plus any msgtype invented after this release. Carried out of the sync rather than
    filtered away because R1.6 makes an event that cannot be mapped something the operator has to
    be told about; `sync` is what tells them.

    No body: what a media event's `body` holds is a filename, and repeating it back would read as
    though the thing had been understood.
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
    # Defaulted because a sync with nothing unreadable in it is the ordinary case, and every
    # caller that constructs one by hand means exactly that.
    unmappable: tuple[UnmappableEvent, ...] = ()


def _msgtype(event: Event | BadEvent) -> str | None:
    """The `msgtype` of an `m.room.message`, or None for anything else in a timeline.

    Read off the raw source rather than off nio's parsed class, because the events this exists to
    catch are the ones nio models least: an msgtype it does not know becomes `RoomMessageUnknown`
    with no shared base to check, and content that fails its schema becomes a `BadEvent` with no
    msgtype attribute at all — while both carry it right there in `source`.

    None covers three cases that all mean "not a message": a state event (membership, topic), a
    non-message event (a reaction), and a redaction — which keeps its type and loses its content,
    so there is nothing left to read and nothing the operator wants told back to them.
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
        formatting, and it is what a plain-text reader should see (R11.7).

        `txn_id` makes the send idempotent: a retry with the same value is deduplicated by
        the homeserver rather than posting twice. `EventTag.transaction_id` is where a caller
        gets one, and says which events have a value worth repeating.
        """
        return await self._send(token, room_id, "m.text", body, txn_id, tag, formatted=to_formatted_body(body))

    async def send_notice(self, token: str, room_id: str, body: str, txn_id: str, tag: EventTag) -> str:
        """Send an `m.notice`, returning its event ID.

        Lifecycle and status messages are notices rather than plain text (R7) so clients
        style them apart from Haku's answers and bots ignore them by convention.
        """
        return await self._send(token, room_id, NOTICE_MSGTYPE, body, txn_id, tag)

    async def edit_notice(self, token: str, room_id: str, event_id: str, body: str, txn_id: str, tag: EventTag) -> None:
        """Replace an earlier notice in place, rather than posting a second one.

        This is what lets a turn have **one** status line instead of a line per step (R6.5).
        The edit is its own event carrying `m.replace`: clients that understand it re-render
        the original, and clients that do not show the fallback body — which is why the
        top-level `body` is the new text prefixed with `*`, per the spec's convention, rather
        than the new text alone.
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

        The homeserver expires a typing notice by itself after `TYPING_TIMEOUT_MS`, which is what
        makes this safe: a console that dies mid-turn leaves an indicator that goes away on its
        own rather than one stuck on forever. The cost is that a live turn has to say it again
        before that expiry, which the turn's status driver does.
        """
        self._client.access_token = token
        _unwrap(await self._client.room_typing(room_id, active, timeout=TYPING_TIMEOUT_MS), RoomTypingResponse)

    async def redact(self, token: str, room_id: str, event_id: str, reason: str) -> None:
        """Remove an event. Used to retire a status line once its answer has posted (R6.5).

        A redaction rather than a final edit: the status described work in progress, and once
        the answer is in the room the line is not stale so much as spent — leaving one edited
        to "done" behind on every turn is the clutter the single status line exists to avoid.
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

        The decisions that live here, each load-bearing:

        - **Our own events are dropped first** (R1.5). Doing it here rather than over the result
          is what keeps a notice about an unreadable event from being an unreadable event's worth
          of notice: everything this console posts is excluded before anything is classified.
        - **`m.emote` is prose and is serviced.** It is `m.text` phrased in the third person —
          "/me is looking at the logs" — with the words in `body` where they can be read.
        - **`m.notice` is ignored rather than reported.** It is the msgtype for automated clients,
          which is what Haku's own status, lifecycle and unreadable-event lines go out under. The
          sender rule above already excludes ours; this excludes anything else's, so no notice in
          this room can ever produce a notice about it, from any sender. That is the second of the
          two independent guards against a self-feeding loop, and the reason there is a second one
          is that this is the failure whose cost is unbounded.

        Everything else that is an `m.room.message` is unmappable: the meaning is in an attachment
        or in an msgtype invented after this release, and either way the operator gets told rather
        than nothing happening (R1.6).
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
            # A truncated timeline is the gap that would otherwise silently swallow whatever
            # arrived while the console was down — the very thing the watermark exists to
            # prevent (R1.7). On the first sync there is no range to recover, only a
            # position to take.
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
