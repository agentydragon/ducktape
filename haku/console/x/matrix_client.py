"""Matrix client for the console's chat surface, over `matrix-nio`.

Three deviations from stock nio:

- **Sync position lives in Postgres**, not nio's on-disk store, because the loop can move
  to a different pod (`matrix_sync.MatrixSyncStore`). `since` is always passed explicitly
  and `store_sync_tokens` stays off.
- **Failures raise.** nio reports them as result-union values (`SyncError`); the loop needs
  a rejected token to be distinguishable from a transport failure, so every call here
  converts errors to exceptions.
- **429s are bounded** (`MAX_RATE_LIMIT_RETRIES`), so being rate-limited is something the
  console finds out about rather than something it silently waits out.

The first two are forced by the console being a leader-elected replica set rather than a
single long-lived process; the third by `matrix_pacer` needing to hear the answer.

E2EE is off (`haku/plans/matrix_chat_runtime.md` — the room is a plain DM), so no crypto
store, no `python-olm`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from nio import AsyncClient, AsyncClientConfig, ErrorResponse, Response
from nio.api import MessageDirection
from nio.events.invite_events import InviteMemberEvent
from nio.events.room_events import RoomMessageText
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

from haku.console.x.matrix_markdown import to_formatted_body

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
# **Gotcha: nio's default is unlimited, not off.** `AsyncClient._send` already loops on
# `M_LIMIT_EXCEEDED`, sleeping the server's `retry_after_ms` (or five seconds when it gives
# none), and `max_limit_exceeded=None` means it does that forever — so a rate-limited send
# never returned an error, it just stopped returning. That is worse than a visible failure:
# the caller is blocked inside `room_send` with nothing in any log, and `matrix_pacer` cannot
# learn a budget it is never told about. Two retries keep a single burst invisible while
# making a sustained one arrive somewhere it can be acted on.
MAX_RATE_LIMIT_RETRIES = 2


# The console's own key inside an event's `content`. Reverse-DNS on the deployment's domain,
# which is the Matrix convention for a field outside the spec's namespace.
HAKU_CONTENT_KEY = "works.allegedly.haku"


class RoomEventKind(StrEnum):
    """What the console meant by an event it sent.

    Every one of these used to be inferred. "Is this conversational" was read off the msgtype —
    true only because notices happen to be the things worth excluding — and "which transcript row
    is this" could not be asked at all. A kind says it instead.
    """

    REPLY = "reply"
    STATUS = "status"
    NARRATION = "narration"
    LIFECYCLE = "lifecycle"
    HOLDING = "holding"
    ROOM = "room"


@dataclass(frozen=True)
class EventTag:
    """What the console states about an event it is sending.

    **Ids and kinds only.** This room is public and federated, so the content travels to every
    server in it and is not ours to take back; a tag that carried text would be publishing the
    same thing twice, in a field nobody renders. Redaction strips it along with the rest of
    `content`, which is the behaviour a status line's retirement wants anyway.

    `message_id` is the transcript row and `agent_message_id` is the agent's own `msg_…`. Both are
    absent on everything that is not a reply, because nothing else corresponds to a row.
    """

    kind: RoomEventKind
    session_id: UUID | None = None
    message_id: UUID | None = None
    agent_message_id: str | None = None

    def content(self) -> dict[str, Any]:
        """The tag as it goes on the wire, with absent fields left out rather than sent null."""
        stated: dict[str, Any] = {"kind": self.kind}
        if self.session_id is not None:
            stated["session_id"] = str(self.session_id)
        if self.message_id is not None:
            stated["message_id"] = str(self.message_id)
        if self.agent_message_id is not None:
            stated["agent_message_id"] = self.agent_message_id
        return stated

    @classmethod
    def parse(cls, content: dict[str, Any]) -> EventTag | None:
        """Read a tag off an event, or None for anything this console did not tag.

        None covers two different things and deliberately does not distinguish them: an event
        somebody else sent, and one this console sent before tagging existed. Neither can be
        interpreted, so both fall back to the msgtype and sender rules that predate this.
        """
        if not isinstance(stated := content.get(HAKU_CONTENT_KEY), dict):
            return None
        try:
            kind = RoomEventKind(stated["kind"])
        except (KeyError, ValueError):
            # A kind this release does not know is a newer console talking, which is the one
            # case where guessing is worse than admitting we cannot read it.
            logger.warning("Matrix: unreadable Haku tag %r", stated)
            return None
        return cls(
            kind=kind,
            session_id=_optional_uuid(stated.get("session_id")),
            message_id=_optional_uuid(stated.get("message_id")),
            agent_message_id=stated.get("agent_message_id"),
        )


def _optional_uuid(value: Any) -> UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return UUID(value)
    except ValueError:
        logger.warning("Matrix: Haku tag carried an unparseable id %r", value)
        return None


class MatrixError(Exception):
    """The homeserver returned an error for a call we made.

    `retry_after_ms` is a 429's own answer to "how long", carried rather than formatted into
    the message because `matrix_pacer` acts on it: it is the only measurement of the room's
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
    """One `m.room.message` addressed to us."""

    room_id: str
    event_id: str
    sender: str
    body: str
    origin_server_ts: int
    # Present only on events this console sent since tagging landed; see `EventTag.parse`.
    tag: EventTag | None = None


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


def _is_conversational(message: InboundMessage) -> bool:
    """Whether *message* is part of the conversation rather than the console talking about it.

    An untagged event is the operator's, or predates tagging — either way the msgtype rule that
    got it this far is the only reading available, and it already said yes.
    """
    return message.tag is None or message.tag.kind is RoomEventKind.REPLY


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

        Lifecycle notices are excluded twice over. They go out as `m.notice`, which nio parses
        as `RoomMessageNotice` — a different class from the `RoomMessageText` this keeps — and
        they now also carry a kind that says they are not the conversation. The msgtype was
        doing that job by coincidence: it is true only for as long as everything worth excluding
        happens to be a notice, which is a property of today's senders rather than a rule.

        `since` is a `/sync` watermark, which is also a valid `/messages` pagination token —
        so this reads back from wherever the loop has got to, with no second position to keep.

        **`limit` counts messages, not timeline events.** It used to be the page size, with the
        filter applied afterwards — and this room's timeline is mostly the console talking to
        itself: a provisioning announcement and one notice per line of bootstrap output on every
        session start, plus the status line's creation, edits and redaction on every turn. Each
        of those is excluded here, correctly, and each still spent one of the twenty events
        fetched. A re-awakening could come back with two or three real messages, or none, while
        believing it had asked for twenty — silently, since the prompt renders with whatever it
        found. Paging until the count is met is the same shape `_backfill` beside this already
        uses.
        """
        self._client.access_token = token
        recent: list[InboundMessage] = []
        start = since
        for _ in range(MAX_BACKFILL_PAGES):
            page = _unwrap(
                await self._client.room_messages(
                    room_id, start=start, direction=MessageDirection.back, limit=TIMELINE_LIMIT
                ),
                RoomMessagesResponse,
            )
            recent.extend(
                message
                for event in page.chunk
                if isinstance(event, RoomMessageText)
                if _is_conversational(message := self._inbound(room_id, event))
            )
            # `end` is absent at the start of the room's history: there is no earlier page to ask
            # for, and asking again would re-read this one forever.
            if len(recent) >= limit or not page.chunk or page.end is None:
                break
            start = page.end
        recent.reverse()
        return tuple(recent[-limit:] if len(recent) > limit else recent)

    async def send_text(self, token: str, room_id: str, body: str, txn_id: str, tag: EventTag) -> str:
        """Send Haku's reply, rendering its Markdown for clients that display HTML.

        `body` stays the Markdown source: it is the spec's fallback for clients that show no
        formatting, and it is what a plain-text reader should see (R11.7).

        `txn_id` makes the send idempotent: a retry with the same value is deduplicated by
        the homeserver rather than posting twice.
        """
        return await self._send(token, room_id, "m.text", body, txn_id, tag, formatted=to_formatted_body(body))

    async def send_notice(self, token: str, room_id: str, body: str, txn_id: str, tag: EventTag) -> str:
        """Send an `m.notice`, returning its event ID.

        Lifecycle and status messages are notices rather than plain text (R7) so clients
        style them apart from Haku's answers and bots ignore them by convention.
        """
        return await self._send(token, room_id, "m.notice", body, txn_id, tag)

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
        new_content: dict[str, Any] = {"msgtype": "m.notice", "body": body, HAKU_CONTENT_KEY: tag.content()}
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
            tag=EventTag.parse(event.source.get("content", {})),
        )
