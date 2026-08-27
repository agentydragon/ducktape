"""Fold the conversation stream into a room's live status and typing state.

The turn process records facts; it never drives a channel. A Matrix subscriber replays those facts,
keeps this fold current, and periodically reconciles its ephemeral typing indicator and editable
status line. The fold has no task of its own, so a replica takeover reconstructs the same state from
the durable stream instead of inheriting a process-local poller.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from itertools import groupby
from typing import Protocol
from uuid import UUID

from haku.console.chat_models import ItemType
from haku.console.x.session_events import (
    LeaseExpiredBody,
    MessageCompletedBody,
    MessageStartedBody,
    ReasoningCompletedBody,
    ReasoningStartedBody,
    SegmentBody,
    SessionAdoptedBody,
    SessionEndedBody,
    SessionProvisioningBody,
    ToolCallCompletedBody,
    ToolCallStartedBody,
    TurnAbortedBody,
    TurnAnsweredBody,
    TurnFailedBody,
    TurnStartedBody,
)
from haku.console.x.subscription import StreamedEvent

# Below this the answer itself is the status, and a status/answer pair is clutter.
STATUS_AFTER = timedelta(seconds=8)

# Synapse expires typing after 30 seconds. Refresh comfortably inside that deadline.
TYPING_REFRESH = timedelta(seconds=10)

# Floor status edits for a reader and for the room's send budget. Changes are deferred, not lost.
STATUS_EDIT_INTERVAL = timedelta(seconds=5)

# An active turn needs a clock tick even when it emits no events: typing expires and the status line
# appears only after the lazy threshold. The subscriber owns this tick, not the turn process.
ACTIVE_TICK_SECONDS = 1.0

PROVISIONING_STATUS = "provisioning a sandbox"


class StatusFrontend(Protocol):
    """The Matrix operations the stream fold reconciles."""

    async def show_status(self, text: str) -> None: ...

    async def clear_status(self) -> None: ...

    async def set_typing(self, active: bool) -> None: ...


def coarse_status(events: Sequence[StreamedEvent], open_items: dict[UUID, ItemType]) -> str | None:
    """The most specific room status in one stored event run.

    Rows projected from one frame share ``created_at``. A tool start and the message completion
    beside it are therefore one run, where the tool wins rather than being buried by ``writing``.
    """
    if names := [event.body.tool_name for event in events if isinstance(event.body, ToolCallStartedBody)]:
        return f"running {', '.join(names)}"
    if any(_the_agent_writing(event, open_items) for event in events):
        return "writing"
    return None


def _the_agent_writing(event: StreamedEvent, open_items: dict[UUID, ItemType]) -> bool:
    match event.body:
        case MessageStartedBody() | ReasoningStartedBody() | MessageCompletedBody():
            return True
        case SegmentBody():
            return event.item_id is not None and open_items.get(event.item_id) in {ItemType.MESSAGE, ItemType.REASONING}
        case _:
            return False


class LiveStatus:
    """Current channel-visible state, derived only from ordered conversation events."""

    def __init__(self) -> None:
        self._session_id: UUID | None = None
        self._turn_id: UUID | None = None
        self._turn_started_at: datetime | None = None
        self._provisioning = False
        self._state: str | None = None
        self._open_items: dict[UUID, ItemType] = {}

        # What this process has reconciled to Matrix. These are delivery latches, not authorities:
        # a new leader rebuilds the desired half above from the stream before consulting them.
        self._shown: str | None = None
        self._shown_at: datetime | None = None
        self._typing = False
        self._typed_at: datetime | None = None
        self._settled = False

    @property
    def active(self) -> bool:
        return self._turn_id is not None

    @property
    def tick_seconds(self) -> float | None:
        return ACTIVE_TICK_SECONDS if self.active else None

    def apply(self, events: Sequence[StreamedEvent]) -> None:
        """Fold ordered rows. Calling this again with the same prefix reaches the same desired state."""
        for _, grouped in groupby(events, key=lambda event: event.created_at):
            run = tuple(grouped)
            self._start(run)
            if self.active and (state := coarse_status(run, self._open_items)) is not None:
                self._state = state
            self._complete_items(run)
            self._end(run)

    def _start(self, events: Sequence[StreamedEvent]) -> None:
        for event in events:
            match event.body:
                case SessionProvisioningBody():
                    self._session_id = event.session_id
                    self._turn_id = None
                    self._turn_started_at = None
                    self._provisioning = True
                    self._state = None
                    self._open_items.clear()
                case SessionAdoptedBody() if event.session_id == self._session_id:
                    self._provisioning = False
                    self._state = None
                case TurnStartedBody():
                    self._session_id = event.session_id
                    self._turn_id = event.turn_id
                    self._turn_started_at = event.created_at
                    self._provisioning = False
                    self._state = None
                    self._open_items.clear()
                case MessageStartedBody() if event.item_id is not None:
                    self._open_items[event.item_id] = ItemType.MESSAGE
                case ReasoningStartedBody() if event.item_id is not None:
                    self._open_items[event.item_id] = ItemType.REASONING
                case ToolCallStartedBody() if event.item_id is not None:
                    self._open_items[event.item_id] = ItemType.TOOL_CALL
                case _:
                    pass

    def _complete_items(self, events: Sequence[StreamedEvent]) -> None:
        for event in events:
            if event.item_id is not None and isinstance(
                event.body, (MessageCompletedBody, ReasoningCompletedBody, ToolCallCompletedBody)
            ):
                self._open_items.pop(event.item_id, None)

    def _end(self, events: Sequence[StreamedEvent]) -> None:
        for event in events:
            match event.body:
                case TurnAnsweredBody() | TurnAbortedBody() | TurnFailedBody() if event.turn_id == self._turn_id:
                    self._turn_id = None
                    self._turn_started_at = None
                    self._state = None
                    self._open_items.clear()
                # The session guard is load-bearing for `session_ended`: a `CLOSING` session has
                # already left `OPEN_SESSION_STATUSES`, so a replacement session may start — and
                # write its events — while the old session's `session_ended` waits on its claim
                # cleanup (`request_close` writes no event; `complete_claim_cleanup` does). That
                # late ending must not wipe the successor's live state. `lease_expired` alone could
                # never arrive late: the sweep expires only leased statuses, all of which are open,
                # an open session blocks every replacement path, and the event commits in the
                # transaction that ends the session.
                case SessionEndedBody() | LeaseExpiredBody() if event.session_id == self._session_id:
                    self._session_id = None
                    self._turn_id = None
                    self._turn_started_at = None
                    self._provisioning = False
                    self._state = None
                    self._open_items.clear()
                case _:
                    pass

    async def reconcile(self, frontend: StatusFrontend, *, now: datetime | None = None) -> None:
        """Bring Matrix to the folded state; safe to call on every subscriber pass."""
        now = now or datetime.now(UTC)

        if self.active:
            if self._typed_at is None or now - self._typed_at >= TYPING_REFRESH:
                await frontend.set_typing(True)
                self._typing = True
                self._typed_at = now
        elif self._typing or not self._settled:
            await frontend.set_typing(False)
            self._typing = False
            self._typed_at = None

        desired = self._desired(now)
        if desired is None:
            if self._shown is not None or not self._settled:
                await frontend.clear_status()
                self._shown = None
                self._shown_at = None
        elif desired != self._shown and (self._shown_at is None or now - self._shown_at >= STATUS_EDIT_INTERVAL):
            await frontend.show_status(desired)
            self._shown = desired
            self._shown_at = now

        self._settled = True

    def _desired(self, now: datetime) -> str | None:
        if self._provisioning:
            return PROVISIONING_STATUS
        if (
            self.active
            and self._state is not None
            and self._turn_started_at is not None
            and now - self._turn_started_at >= STATUS_AFTER
        ):
            return self._state
        return None
