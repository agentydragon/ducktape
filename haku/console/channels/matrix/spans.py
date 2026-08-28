"""The room's editable notices as spans of the conversation, folded from the stream.

A **span** is the stretch of the conversation one editable room notice summarises — a turn's work,
a session's pre-turn life — and its identity is the event that opened it: `Span.opened_seq` names a
durable `conversation_event` row, so the notice's tag carries a `ConversationEventSource` the
correspondence reader can hold, and the revision-log subject derived from it survives every process
that edits the line. What the room is told is never addressed by a runner incarnation.

`LiveSpans` is the pure fold: ordered `StreamedEvent`s in, the currently open spans and their
bounded bodies out, plus a `SpanClose` per closing event saying whether the line is **sealed** (a
final edit that stays in scrollback — a session that ended) or **retired** (a redaction — live
state that is spent, like a finished turn's status line). The fold owns no I/O; `reconcile` walks
the desired state against what it last sent and drives a `RoomFrontend`, which is where Matrix
begins.

Two spans exist at most:

- **One work span per turn.** What is happening now — the running tools, or prose being written —
  plus a bounded tally of the tool calls already done. Forty calls summarise to a tally and the one
  in flight, never to forty lines. Created lazily after `STATUS_AFTER` so short exchanges leave no
  status/answer pair, edited at most once per `STATUS_EDIT_INTERVAL`, retired when the turn ends.
- **One lifecycle span per session, while nothing else shows the session is alive.** Provisioning,
  setup narration and adoption edit one line instead of posting one notice each; the first turn
  retires it, because a conversation that is moving is its own evidence. A lease expiry seals the
  session's ending into scrollback — as the span's final edit when the line still shows, and as a
  one-event span of its own when it does not, which is exactly the sealed notice this generalises.

Replay is the fold's own problem: `advance` is idempotent by position, and the `SpanClose` for an
already-folded event is answered from memory (pruned by `prune` once the caller's cursor has
covered it), so a batch replayed after a partial failure closes the same spans instead of none.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from haku.console.chat_models import LeaseExpiryReason
from haku.console.x.session_events import (
    LeaseExpiredBody,
    MessageCompletedBody,
    MessageStartedBody,
    PromptCompletedBody,
    PromptRejectedBody,
    PromptStartedBody,
    ReasoningCompletedBody,
    ReasoningStartedBody,
    SegmentBody,
    SessionAdoptedBody,
    SessionEndedBody,
    SessionProvisioningBody,
    SetupNarrationBody,
    ToolCallCompletedBody,
    ToolCallStartedBody,
    TurnAbortedBody,
    TurnAnsweredBody,
    TurnFailedBody,
    TurnStartedBody,
    UnknownEventBody,
    UnreadableInputBody,
)
from haku.console.x.subscription import START, StreamedEvent, StreamPosition

# Below this the answer itself is the status, and a status/answer pair is clutter.
STATUS_AFTER = timedelta(seconds=8)

# Synapse expires typing after 30 seconds. Refresh comfortably inside that deadline.
TYPING_REFRESH = timedelta(seconds=10)

# Floor span edits for a reader and for the room's send budget. Changes are deferred, not lost:
# the reconciler recomputes the difference on its next pass and sends the current truth.
STATUS_EDIT_INTERVAL = timedelta(seconds=5)

# An active turn needs a clock tick even when it emits no events: typing expires and the status line
# appears only after the lazy threshold. The subscriber owns this tick, not the turn process.
ACTIVE_TICK_SECONDS = 1.0

PROVISIONING_STATUS = "provisioning a sandbox"


class SpanKind(StrEnum):
    """Which stretch of the conversation a span summarises."""

    TURN = "turn"
    SESSION = "session"


@dataclass(frozen=True, slots=True)
class Span:
    """One editable notice's durable identity: the event that opened its span.

    `opened_seq` is a `conversation_event.event_seq`, so the identity survives every session and
    every console process, and the same triple the sealed notices already tag —
    attachment, conversation, position — covers the editable copy too.
    """

    kind: SpanKind
    conversation_id: UUID
    opened_seq: int

    @property
    def subject(self) -> str:
        """What the revision log calls this span's room event."""
        return f"{self.kind}:{self.opened_seq}"


@dataclass(frozen=True, slots=True)
class SealSpan:
    """Close this span with a final edit that stays in scrollback."""

    span: Span
    body: str


@dataclass(frozen=True, slots=True)
class RetireSpan:
    """Withdraw this span's line: what it showed was live state, and the state is spent."""

    span: Span


type SpanClose = SealSpan | RetireSpan


class RoomFrontend(Protocol):
    """The Matrix operations the fold's output is reconciled through.

    Implemented by the sync service, which holds the credential and the pacer; consumed by
    `LiveSpans.reconcile` and the conversation subscriber. Every call names the room and the
    attachment because the caller resolved them once per pass; nothing here reads a binding.

    A Protocol rather than `SyncService`, its production implementer: naming the service
    here is an import cycle (`sync` imports `Span`), and the pure-fold tests fake Matrix at
    exactly this boundary.
    """

    async def show_span(self, room_id: str, attachment_id: UUID, span: Span, body: str) -> None: ...

    async def seal_span(self, room_id: str, attachment_id: UUID, span: Span, body: str) -> None: ...

    async def retire_span(self, room_id: str, attachment_id: UUID, span: Span) -> None: ...

    async def retire_stale_spans(self, room_id: str, attachment_id: UUID, keep: frozenset[str]) -> None: ...

    async def set_typing(self, room_id: str, active: bool) -> None: ...


def _why_it_lapsed(reason: LeaseExpiryReason) -> str:
    """The room's own words for a lease that ran out, not `session_store._expiry_detail`'s.

    The holder is left out on purpose: a replica name is the console's own topology, and what the
    operator can act on is that the session is gone.
    """
    match reason:
        case LeaseExpiryReason.HOLDER_GONE:
            return "the console replica serving it went away"
        case LeaseExpiryReason.UNADOPTED:
            return "its sandbox went away and nothing took it back over"
        case LeaseExpiryReason.NEVER_ATTACHED:
            return "its sandbox never came up"


@dataclass(slots=True)
class _OpenTurn:
    span: Span
    turn_id: UUID | None
    session_id: UUID | None
    started_at: datetime
    # The turn's items still open, each with its tool name — None for prose (a message or a
    # stretch of reasoning), whose activity is "writing" either way.
    open_items: dict[UUID, str | None] = field(default_factory=dict)
    # Sticky: the last state worth showing, kept between items so the line never goes blank
    # mid-turn. Reset only by the span closing.
    activity: str | None = None
    tools_done: int = 0

    def body(self) -> str | None:
        if self.activity is None:
            return None
        if self.tools_done == 0:
            return self.activity
        calls = "tool call" if self.tools_done == 1 else "tool calls"
        return f"{self.activity} — {self.tools_done} {calls} done"


@dataclass(slots=True)
class _OpenSession:
    span: Span
    session_id: UUID | None
    body: str


@dataclass(frozen=True, slots=True)
class _Shown:
    body: str
    at: datetime


class LiveSpans:
    """The open spans of one conversation, derived only from ordered conversation events."""

    def __init__(self, conversation_id: UUID) -> None:
        self.conversation_id = conversation_id
        self._turn: _OpenTurn | None = None
        self._session: _OpenSession | None = None
        self._folded_through = START
        # The close decisions for positions the cursor has not covered yet, so a batch replayed
        # after a partial failure gets the same answers instead of none.
        self._closes: dict[int, tuple[SpanClose, ...]] = {}

        # What this process has reconciled to Matrix. Delivery latches, not authorities: a new
        # leader rebuilds the desired half above from the stream and repairs the room by sweep.
        self._shown: dict[str, _Shown] = {}
        self._typing = False
        self._typed_at: datetime | None = None
        self._settled = False

    @property
    def folded_through(self) -> StreamPosition:
        """How far this fold has read — the position `advance` is idempotent up to."""
        return self._folded_through

    @property
    def active(self) -> bool:
        return self._turn is not None

    @property
    def tick_seconds(self) -> float | None:
        return ACTIVE_TICK_SECONDS if self.active else None

    def open_subjects(self) -> frozenset[str]:
        """The revision subjects still legitimately live — everything else is the sweep's."""
        return frozenset(open_span.span.subject for open_span in (self._turn, self._session) if open_span is not None)

    def advance(self, event: StreamedEvent) -> tuple[SpanClose, ...]:
        """Fold one row, returning what it closes. Idempotent by position.

        A position already folded is answered from the recorded closes rather than re-folded, so a
        replay cannot double-count a tally or find a span already closed by its own first pass.
        """
        seq = event.position.event_seq
        if seq <= self._folded_through.event_seq:
            return self._closes.get(seq, ())
        closes = self._transition(event)
        if closes:
            self._closes[seq] = closes
        self._folded_through = event.position
        return closes

    def prune(self, through: StreamPosition) -> None:
        """Forget close decisions the caller's cursor has covered: they can no longer replay."""
        self._closes = {seq: closes for seq, closes in self._closes.items() if seq > through.event_seq}

    def _transition(self, event: StreamedEvent) -> tuple[SpanClose, ...]:
        match event.body:
            case SessionProvisioningBody():
                return self._open_session(event, PROVISIONING_STATUS)
            case SessionAdoptedBody(holder=holder):
                return self._open_session(event, f"another console replica ({holder}) took this session over")
            case SetupNarrationBody(text=text):
                return self._open_session(event, text)
            case SessionEndedBody():
                # An orderly ending is live state spent, not news: the pre-span rendering never
                # announced it either, and a lease expiry still seals the thread's account below.
                return self._end_session(event, seal_body=None)
            case LeaseExpiredBody(reason=reason):
                return self._end_session(event, seal_body=f"the session ended — {_why_it_lapsed(reason)}")
            case TurnStartedBody():
                closed = self._retire_turn()
                if self._session is not None:
                    # A moving conversation is its own evidence of life, so the pre-turn
                    # lifecycle line is spent the moment the first turn opens.
                    closed += (RetireSpan(span=self._session.span),)
                    self._drop(self._session.span)
                    self._session = None
                self._turn = _OpenTurn(
                    span=Span(
                        kind=SpanKind.TURN, conversation_id=self.conversation_id, opened_seq=event.position.event_seq
                    ),
                    turn_id=event.turn_id,
                    session_id=event.session_id,
                    started_at=event.created_at,
                )
                return closed
            case MessageStartedBody() | ReasoningStartedBody() if self._turn is not None and event.item_id is not None:
                self._turn.open_items[event.item_id] = None
                self._refresh_activity()
            case ToolCallStartedBody(tool_name=tool_name) if self._turn is not None and event.item_id is not None:
                self._turn.open_items[event.item_id] = tool_name
                self._refresh_activity()
            case MessageCompletedBody() | ReasoningCompletedBody() if self._turn is not None:
                if event.item_id is not None:
                    self._turn.open_items.pop(event.item_id, None)
                self._refresh_activity()
            case ToolCallCompletedBody() if self._turn is not None:
                if event.item_id is not None:
                    self._turn.open_items.pop(event.item_id, None)
                self._turn.tools_done += 1
                self._refresh_activity()
            case TurnAnsweredBody() | TurnAbortedBody() | TurnFailedBody() if (
                self._turn is not None and event.turn_id == self._turn.turn_id
            ):
                return self._retire_turn()
            case (
                SegmentBody()
                | PromptStartedBody()
                | PromptCompletedBody()
                | PromptRejectedBody()
                | UnreadableInputBody()
                | MessageStartedBody()
                | ReasoningStartedBody()
                | ToolCallStartedBody()
                | MessageCompletedBody()
                | ReasoningCompletedBody()
                | ToolCallCompletedBody()
                | TurnAnsweredBody()
                | TurnAbortedBody()
                | TurnFailedBody()
                | UnknownEventBody()
            ):
                pass
        return ()

    def _open_session(self, event: StreamedEvent, body: str) -> tuple[SpanClose, ...]:
        if self._session is not None and self._session.session_id == event.session_id:
            self._session.body = body
            return ()
        closed: tuple[SpanClose, ...] = ()
        if self._session is not None:
            # A new session's life beginning while another's line is still up: the old line was
            # live state for a session nothing recorded an ending for, so it is spent, not sealed.
            closed = (RetireSpan(self._session.span),)
            self._drop(self._session.span)
        self._session = _OpenSession(
            span=Span(kind=SpanKind.SESSION, conversation_id=self.conversation_id, opened_seq=event.position.event_seq),
            session_id=event.session_id,
            body=body,
        )
        return closed

    def _end_session(self, event: StreamedEvent, *, seal_body: str | None) -> tuple[SpanClose, ...]:
        # The session guards are load-bearing for `session_ended`: a `CLOSING` session has already
        # left `OPEN_SESSION_STATUSES`, so a replacement session may start — and write its events —
        # while the old session's `session_ended` waits on its claim cleanup (`request_close` writes
        # no event; `complete_claim_cleanup` does). That late ending must not close the successor's
        # spans. `lease_expired` alone could never arrive late: the sweep expires only leased
        # statuses, all of which are open, an open session blocks every replacement path, and the
        # event commits in the transaction that ends the session.
        closed: tuple[SpanClose, ...] = ()
        if self._turn is not None and self._turn.session_id == event.session_id:
            closed += self._retire_turn()
        if self._session is not None and self._session.session_id == event.session_id:
            span = self._session.span
            self._drop(span)
            self._session = None
            closed += (SealSpan(span=span, body=seal_body) if seal_body is not None else RetireSpan(span=span),)
        elif seal_body is not None:
            # No line shows this session, so the ending is a span of one event — which is exactly
            # the sealed notice this generalises, same words, same source, same transaction.
            one_event = Span(
                kind=SpanKind.SESSION, conversation_id=self.conversation_id, opened_seq=event.position.event_seq
            )
            closed += (SealSpan(span=one_event, body=seal_body),)
        return closed

    def _retire_turn(self) -> tuple[SpanClose, ...]:
        if self._turn is None:
            return ()
        span = self._turn.span
        self._drop(span)
        self._turn = None
        return (RetireSpan(span=span),)

    def _drop(self, span: Span) -> None:
        self._shown.pop(span.subject, None)

    def _refresh_activity(self) -> None:
        turn = self._turn
        assert turn is not None
        if names := [name for name in turn.open_items.values() if name is not None]:
            turn.activity = f"running {', '.join(names)}"
        elif turn.open_items:
            turn.activity = "writing"
        # Nothing open keeps the sticky last activity: the line never goes blank mid-turn.

    def _desired(self, now: datetime) -> list[tuple[Span, str]]:
        desired: list[tuple[Span, str]] = []
        if self._session is not None:
            desired.append((self._session.span, self._session.body))
        if (
            self._turn is not None
            and now - self._turn.started_at >= STATUS_AFTER
            and (body := self._turn.body()) is not None
        ):
            desired.append((self._turn.span, body))
        return desired

    async def reconcile(
        self, frontend: RoomFrontend, room_id: str, attachment_id: UUID, *, now: datetime | None = None
    ) -> None:
        """Bring the room's live lines and typing to the folded state; safe on every pass."""
        now = now or datetime.now(UTC)

        if self.active:
            if self._typed_at is None or now - self._typed_at >= TYPING_REFRESH:
                await frontend.set_typing(room_id, True)
                self._typing = True
                self._typed_at = now
        elif self._typing or not self._settled:
            await frontend.set_typing(room_id, False)
            self._typing = False
            self._typed_at = None

        for span, body in self._desired(now):
            shown = self._shown.get(span.subject)
            if shown is not None and (shown.body == body or now - shown.at < STATUS_EDIT_INTERVAL):
                continue
            await frontend.show_span(room_id, attachment_id, span, body)
            self._shown[span.subject] = _Shown(body=body, at=now)

        self._settled = True
