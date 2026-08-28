"""The span fold: conversation events in, bounded editable lines and their closes out.

Pure by design — no room, no homeserver, no database — because these are the cases an end-to-end
test cannot provoke on demand: a forty-call run collapsing to a tally, a session replaced mid-turn,
a turn aborting between two tool results, a replayed batch closing the same spans again.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest_bazel

from haku.console.channels.matrix.spans import (
    PROVISIONING_STATUS,
    STATUS_AFTER,
    STATUS_EDIT_INTERVAL,
    LiveSpans,
    RetireSpan,
    SealSpan,
    Span,
    SpanKind,
)
from haku.console.chat_models import LeaseExpiryReason, SessionStatus, ToolOutcome
from haku.console.session.subscription import StreamedEvent, StreamPosition
from haku.console.x.session_events import (
    LeaseExpiredBody,
    MessageCompletedBody,
    MessageStartedBody,
    SessionEndedBody,
    SessionProvisioningBody,
    SetupNarrationBody,
    ToolCallCompletedBody,
    ToolCallStartedBody,
    TurnAbortedBody,
    TurnAnsweredBody,
    TurnStartedBody,
)

CONVERSATION = UUID("00000000-0000-4000-8000-00000000c0c0")
SESSION = UUID("11111111-1111-4111-8111-111111111111")
TURN = UUID("22222222-2222-4222-8222-222222222222")
MESSAGE = UUID("33333333-3333-4333-8333-333333333333")
ROOM = "!room:allegedly.works"
ATTACHMENT = UUID("44444444-4444-4444-8444-444444444444")
STARTED = datetime(2026, 8, 19, tzinfo=UTC)

TURN_SPAN = Span(kind=SpanKind.TURN, conversation_id=CONVERSATION, opened_seq=1)
SESSION_SPAN = Span(kind=SpanKind.SESSION, conversation_id=CONVERSATION, opened_seq=1)


class _RecordingFrontend:
    def __init__(self) -> None:
        self.shown: list[tuple[str, str]] = []
        self.sealed: list[tuple[str, str]] = []
        self.retired: list[str] = []
        self.swept: list[frozenset[str]] = []
        self.typed: list[bool] = []

    async def show_span(self, room_id: str, attachment_id: UUID, span: Span, body: str) -> None:
        self.shown.append((span.subject, body))

    async def seal_span(self, room_id: str, attachment_id: UUID, span: Span, body: str) -> None:
        self.sealed.append((span.subject, body))

    async def retire_span(self, room_id: str, attachment_id: UUID, span: Span) -> None:
        self.retired.append(span.subject)

    async def retire_stale_spans(self, room_id: str, attachment_id: UUID, keep: frozenset[str]) -> None:
        self.swept.append(keep)

    async def set_typing(self, room_id: str, active: bool) -> None:
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


def _tool(name: str, *, seq: int, item_id: UUID, at: datetime = STARTED) -> StreamedEvent:
    return _event(
        ToolCallStartedBody(call_id=f"call-{seq}", tool_name=name, arguments={}), seq=seq, at=at, item_id=item_id
    )


def _tool_done(*, seq: int, item_id: UUID, at: datetime = STARTED) -> StreamedEvent:
    return _event(ToolCallCompletedBody(structured={}, outcome=ToolOutcome.SUCCEEDED), seq=seq, at=at, item_id=item_id)


def _apply(state: LiveSpans, *events: StreamedEvent) -> list[SealSpan | RetireSpan]:
    closed: list[SealSpan | RetireSpan] = []
    for event in events:
        closed.extend(state.advance(event))
    return closed


async def _line(state: LiveSpans, *, now: datetime) -> str | None:
    """The one line a fresh reconcile would put up, or None where it puts up none."""
    frontend = _RecordingFrontend()
    await state.reconcile(frontend, ROOM, ATTACHMENT, now=now)
    if not frontend.shown:
        return None
    [(_, body)] = frontend.shown
    return body


async def test_a_running_tool_wins_over_prose_beside_it() -> None:
    state = LiveSpans(CONVERSATION)
    _apply(
        state,
        _event(TurnStartedBody(), seq=1),
        _event(MessageStartedBody(), seq=2, item_id=MESSAGE),
        _tool("Bash", seq=3, item_id=UUID(int=3)),
        _event(MessageCompletedBody(backend_item_id="m"), seq=4, item_id=MESSAGE),
    )

    assert await _line(state, now=STARTED + STATUS_AFTER) == "running Bash"


async def test_only_open_prose_is_writing() -> None:
    state = LiveSpans(CONVERSATION)
    _apply(state, _event(TurnStartedBody(), seq=1), _event(MessageStartedBody(), seq=2, item_id=MESSAGE))

    assert await _line(state, now=STARTED + STATUS_AFTER) == "writing"


async def test_forty_tool_calls_collapse_to_the_one_activity_and_a_tally() -> None:
    """A room event is permanent and federated, and an edit re-publishes its whole body, so the
    line stays bounded however long the run gets."""
    state = LiveSpans(CONVERSATION)
    _apply(state, _event(TurnStartedBody(), seq=1))
    for call in range(40):
        item = UUID(int=1000 + call)
        _apply(state, _tool("Bash", seq=2 + 2 * call, item_id=item), _tool_done(seq=3 + 2 * call, item_id=item))

    assert await _line(state, now=STARTED + STATUS_AFTER) == "running Bash — 40 tool calls done"


async def test_a_short_turn_types_but_never_creates_a_line() -> None:
    frontend = _RecordingFrontend()
    state = LiveSpans(CONVERSATION)
    _apply(state, _event(TurnStartedBody(), seq=1), _tool("Bash", seq=2, item_id=UUID(int=2)))

    await state.reconcile(frontend, ROOM, ATTACHMENT, now=STARTED + STATUS_AFTER - timedelta(seconds=1))
    assert (frontend.typed, frontend.shown) == ([True], [])

    closes = _apply(state, _event(TurnAnsweredBody(), seq=3, at=STARTED + STATUS_AFTER))
    await state.reconcile(frontend, ROOM, ATTACHMENT, now=STARTED + STATUS_AFTER)

    assert frontend.typed == [True, False]
    assert closes == [RetireSpan(span=TURN_SPAN)]


async def test_a_slow_turn_shows_the_latest_coarse_state() -> None:
    frontend = _RecordingFrontend()
    state = LiveSpans(CONVERSATION)
    _apply(state, _event(TurnStartedBody(), seq=1), _tool("Bash", seq=2, item_id=UUID(int=2)))

    await state.reconcile(frontend, ROOM, ATTACHMENT, now=STARTED + STATUS_AFTER)

    assert frontend.shown == [("turn:1", "running Bash")]
    assert state.tick_seconds == 1.0


async def test_a_change_inside_the_edit_floor_is_deferred_not_lost() -> None:
    frontend = _RecordingFrontend()
    state = LiveSpans(CONVERSATION)
    _apply(state, _event(TurnStartedBody(), seq=1), _tool("Read", seq=2, item_id=UUID(int=2)))
    await state.reconcile(frontend, ROOM, ATTACHMENT, now=STARTED + STATUS_AFTER)

    changed_at = STARTED + STATUS_AFTER + timedelta(seconds=1)
    _apply(
        state,
        _tool_done(seq=3, at=changed_at, item_id=UUID(int=2)),
        _tool("Bash", seq=4, at=changed_at, item_id=UUID(int=4)),
    )
    await state.reconcile(frontend, ROOM, ATTACHMENT, now=changed_at)
    assert frontend.shown == [("turn:1", "running Read")]

    await state.reconcile(frontend, ROOM, ATTACHMENT, now=STARTED + STATUS_AFTER + STATUS_EDIT_INTERVAL)
    assert frontend.shown == [("turn:1", "running Read"), ("turn:1", "running Bash — 1 tool call done")]


async def test_provisioning_shows_at_once_and_narration_edits_the_same_line() -> None:
    frontend = _RecordingFrontend()
    state = LiveSpans(CONVERSATION)
    _apply(state, _event(SessionProvisioningBody(), seq=1, turn_id=None))
    await state.reconcile(frontend, ROOM, ATTACHMENT, now=STARTED)

    _apply(state, _event(SetupNarrationBody(text="cloning haku-state"), seq=2, turn_id=None))
    await state.reconcile(frontend, ROOM, ATTACHMENT, now=STARTED + STATUS_EDIT_INTERVAL)

    assert frontend.shown == [("session:1", PROVISIONING_STATUS), ("session:1", "cloning haku-state")]
    assert frontend.typed == [False]


async def test_the_first_turn_retires_the_session_line() -> None:
    """A conversation that is moving is its own evidence of life, so the pre-turn lifecycle line
    is spent the moment the first turn opens."""
    state = LiveSpans(CONVERSATION)
    _apply(state, _event(SessionProvisioningBody(), seq=1, turn_id=None))

    closes = _apply(state, _event(TurnStartedBody(), seq=2))

    assert closes == [RetireSpan(span=SESSION_SPAN)]
    assert state.open_subjects() == frozenset({"turn:2"})


async def test_a_lease_expiry_seals_the_session_line_with_the_ending() -> None:
    state = LiveSpans(CONVERSATION)
    _apply(state, _event(SessionProvisioningBody(), seq=1, turn_id=None))

    closes = _apply(
        state, _event(LeaseExpiredBody(reason=LeaseExpiryReason.NEVER_ATTACHED, last_holder=None), seq=2, turn_id=None)
    )

    assert closes == [SealSpan(span=SESSION_SPAN, body="the session ended — its sandbox never came up")]


async def test_a_lease_expiry_with_no_line_open_is_a_span_of_one_event() -> None:
    """The degenerate seal is exactly the sealed one-event notice this generalises: same words,
    same source position, so the same deterministic Matrix transaction."""
    state = LiveSpans(CONVERSATION)

    closes = _apply(
        state, _event(LeaseExpiredBody(reason=LeaseExpiryReason.HOLDER_GONE, last_holder="pod-a"), seq=7, turn_id=None)
    )

    assert closes == [
        SealSpan(
            span=Span(kind=SpanKind.SESSION, conversation_id=CONVERSATION, opened_seq=7),
            body="the session ended — the console replica serving it went away",
        )
    ]


async def test_a_session_replaced_mid_turn_closes_the_work_span_and_seals_the_ending() -> None:
    state = LiveSpans(CONVERSATION)
    _apply(state, _event(TurnStartedBody(), seq=1), _tool("Bash", seq=2, item_id=UUID(int=2)))

    closes = _apply(
        state, _event(LeaseExpiredBody(reason=LeaseExpiryReason.UNADOPTED, last_holder="pod-a"), seq=3, turn_id=None)
    )

    assert closes == [
        RetireSpan(span=TURN_SPAN),
        SealSpan(
            span=Span(kind=SpanKind.SESSION, conversation_id=CONVERSATION, opened_seq=3),
            body="the session ended — its sandbox went away and nothing took it back over",
        ),
    ]
    assert state.open_subjects() == frozenset()


async def test_an_abort_between_two_tool_results_retires_the_work_span() -> None:
    state = LiveSpans(CONVERSATION)
    first, second = UUID(int=1), UUID(int=2)
    _apply(
        state,
        _event(TurnStartedBody(), seq=1),
        _tool("Bash", seq=2, item_id=first),
        _tool_done(seq=3, item_id=first),
        _tool("Read", seq=4, item_id=second),
    )

    closes = _apply(state, _event(TurnAbortedBody(), seq=5))

    assert closes == [RetireSpan(span=TURN_SPAN)]


async def test_a_replayed_event_answers_the_same_closes_and_counts_nothing_twice() -> None:
    """A batch replayed after a partial failure walks the same rows again; the fold must hand the
    subscriber the same close decisions without double-counting the tally."""
    state = LiveSpans(CONVERSATION)
    item = UUID(int=9)
    done = _tool_done(seq=3, item_id=item)
    ended = _event(TurnAnsweredBody(), seq=4)
    _apply(state, _event(TurnStartedBody(), seq=1), _tool("Bash", seq=2, item_id=item), done)
    first = state.advance(ended)

    assert state.advance(done) == ()
    assert state.advance(ended) == first
    assert first == (RetireSpan(span=TURN_SPAN),)

    state.prune(StreamPosition(4))
    assert state.advance(ended) == ()


async def test_an_orderly_session_end_withdraws_the_line_rather_than_sealing() -> None:
    """Parity with the pre-span rendering: an orderly ending was never announced, and the line is
    live state whose state is over."""
    state = LiveSpans(CONVERSATION)
    _apply(state, _event(SessionProvisioningBody(), seq=1, turn_id=None))

    closes = _apply(state, _event(SessionEndedBody(status=SessionStatus.CLOSED, error=None), seq=2, turn_id=None))

    assert closes == [RetireSpan(span=SESSION_SPAN)]


if __name__ == "__main__":
    pytest_bazel.main()
