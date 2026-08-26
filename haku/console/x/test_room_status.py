"""The conversation-stream fold that drives Matrix live state."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest_bazel

from haku.console.chat_models import SessionStatus, ToolOutcome
from haku.console.x.room_status import (
    PROVISIONING_STATUS,
    STATUS_AFTER,
    STATUS_EDIT_INTERVAL,
    LiveStatus,
    coarse_status,
)
from haku.console.x.session_events import (
    MessageCompletedBody,
    MessageStartedBody,
    SegmentBody,
    SessionAdoptedBody,
    SessionEndedBody,
    SessionProvisioningBody,
    ToolCallCompletedBody,
    ToolCallStartedBody,
    TurnAnsweredBody,
    TurnStartedBody,
)
from haku.console.x.subscription import StreamedEvent, StreamPosition

SESSION = UUID("11111111-1111-4111-8111-111111111111")
TURN = UUID("22222222-2222-4222-8222-222222222222")
MESSAGE = UUID("33333333-3333-4333-8333-333333333333")
TOOL = UUID("44444444-4444-4444-8444-444444444444")
STARTED = datetime(2026, 8, 19, tzinfo=UTC)


class _RecordingFrontend:
    def __init__(self) -> None:
        self.shown: list[str] = []
        self.cleared = 0
        self.typed: list[bool] = []

    async def show_status(self, text: str) -> None:
        self.shown.append(text)

    async def clear_status(self) -> None:
        self.cleared += 1

    async def set_typing(self, active: bool) -> None:
        self.typed.append(active)


def _event(
    body,
    *,
    seq: int,
    at: datetime = STARTED,
    session_id: UUID | None = SESSION,
    turn_id: UUID | None = TURN,
    item_id: UUID | None = None,
) -> StreamedEvent:
    return StreamedEvent(
        position=StreamPosition(seq), session_id=session_id, turn_id=turn_id, item_id=item_id, created_at=at, body=body
    )


def _tool(name: str, *, seq: int, at: datetime = STARTED) -> StreamedEvent:
    return _event(
        ToolCallStartedBody(call_id=f"call-{name}", tool_name=name, arguments={}), seq=seq, at=at, item_id=TOOL
    )


def test_a_tool_call_wins_over_the_message_completion_beside_it() -> None:
    events = (_tool("Bash", seq=1), _event(MessageCompletedBody(backend_item_id="msg"), seq=2, item_id=MESSAGE))

    assert coarse_status(events, {}) == "running Bash"


def test_only_message_prose_is_writing() -> None:
    message_segment = _event(SegmentBody(text="hello"), seq=1, item_id=MESSAGE)
    tool_segment = _event(SegmentBody(text="stdout"), seq=2, item_id=TOOL)

    assert coarse_status((message_segment,), {MESSAGE: MessageStartedBody().item_type}) == "writing"
    assert (
        coarse_status(
            (tool_segment,), {TOOL: ToolCallStartedBody(call_id="c", tool_name="Bash", arguments={}).item_type}
        )
        is None
    )


async def test_provisioning_is_present_tense_state_and_adoption_retires_it() -> None:
    frontend = _RecordingFrontend()
    status = LiveStatus()
    status.apply((_event(SessionProvisioningBody(), seq=1, turn_id=None),))

    await status.reconcile(frontend, now=STARTED)
    assert frontend.shown == [PROVISIONING_STATUS]
    assert frontend.typed == [False]

    status.apply((_event(SessionAdoptedBody(previous_holder=None, holder="console-1"), seq=2, turn_id=None),))
    await status.reconcile(frontend, now=STARTED + timedelta(seconds=1))

    assert frontend.cleared == 1
    assert status.tick_seconds is None


async def test_a_short_turn_types_but_never_creates_a_status_line() -> None:
    frontend = _RecordingFrontend()
    status = LiveStatus()
    status.apply((_event(TurnStartedBody(), seq=1), _tool("Bash", seq=2)))

    await status.reconcile(frontend, now=STARTED + STATUS_AFTER - timedelta(seconds=1))
    assert (frontend.typed, frontend.shown) == ([True], [])

    status.apply((_event(TurnAnsweredBody(), seq=3, at=STARTED + STATUS_AFTER),))
    await status.reconcile(frontend, now=STARTED + STATUS_AFTER)

    assert frontend.typed == [True, False]
    assert frontend.cleared == 1


async def test_a_slow_turn_shows_the_latest_coarse_state() -> None:
    frontend = _RecordingFrontend()
    status = LiveStatus()
    status.apply((_event(TurnStartedBody(), seq=1), _tool("Bash", seq=2)))

    await status.reconcile(frontend, now=STARTED + STATUS_AFTER)

    assert frontend.shown == ["running Bash"]
    assert status.tick_seconds == 1.0


async def test_a_change_inside_the_edit_floor_is_deferred_not_lost() -> None:
    frontend = _RecordingFrontend()
    status = LiveStatus()
    status.apply((_event(TurnStartedBody(), seq=1), _tool("Read", seq=2)))
    await status.reconcile(frontend, now=STARTED + STATUS_AFTER)

    changed_at = STARTED + STATUS_AFTER + timedelta(seconds=1)
    status.apply(
        (
            _event(
                ToolCallCompletedBody(structured={}, outcome=ToolOutcome.SUCCEEDED), seq=3, at=changed_at, item_id=TOOL
            ),
            _tool("Bash", seq=4, at=changed_at),
        )
    )
    await status.reconcile(frontend, now=changed_at)
    assert frontend.shown == ["running Read"]

    await status.reconcile(frontend, now=STARTED + STATUS_AFTER + STATUS_EDIT_INTERVAL)
    assert frontend.shown == ["running Read", "running Bash"]


async def test_a_terminal_session_clears_state_rebuilt_from_the_stream() -> None:
    frontend = _RecordingFrontend()
    status = LiveStatus()
    status.apply(
        (
            _event(TurnStartedBody(), seq=1),
            _tool("Bash", seq=2),
            _event(
                SessionEndedBody(status=SessionStatus.FAILED, error="runner vanished"),
                seq=3,
                at=STARTED + timedelta(seconds=20),
                turn_id=None,
            ),
        )
    )

    await status.reconcile(frontend, now=STARTED + timedelta(seconds=20))

    assert frontend.shown == []
    assert frontend.typed == [False]
    assert frontend.cleared == 1


if __name__ == "__main__":
    pytest_bazel.main()
