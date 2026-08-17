"""Re-project a recorded session's frames and say where `session_events` disagrees.

`check_session` returns findings; it prints nothing and decides nothing, so a caller chooses what
a per-turn disagreement is worth.

**The fold is the write path's own** (`frame_projection.projected`) and the row is built by the
write path's own mapping (`session_events.row`), so the only thing added here is the alignment.
That alignment is **by frame**: a frame's rows are written in one transaction and every event the
write path produces carries `(frame_seq, frame_seq)` as its range, so which rows a frame owns is a
lookup rather than a guess.

**One bound keeps an honest report from being a false one: the cursor.**
`sessions.projected_frame_seq` is how far the fold committed, so a frame past it is one whose
effects were never written — a replica that died mid-turn — and is counted rather than reported.

The ambiguous case is reported rather than guessed at: a turn whose rows begin part-way through
it, which is what a replica on the new image adopting a turn the old one started produces — and
also what a projection that dropped its first frames produces.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from haku.console.chat_models import EventProvenance, StoredEventKind
from haku.console.database_schema import Session, SessionEvent, SessionFrame, SessionTurn
from haku.console.x import frame_projection, session_events
from haku.console.x.setup_output import SETUP_OUTPUT_KIND

# Only ever the `created_at` of a row that is compared against a stored one on every column but
# that, so the value is arbitrary and being a constant keeps two runs of the check identical.
_UNCOMPARED_CLOCK = datetime.fromtimestamp(0, UTC)


@dataclass(frozen=True, slots=True)
class FieldDifference:
    """One column of one row, as the frames project it and as it is stored.

    Rendered to text rather than carried typed: the field set spans an enum, two integers, a string
    and a JSON body, and a caller acts on *that a column differs*, not on the value.
    """

    field: str
    projected: str
    stored: str


@dataclass(frozen=True, slots=True)
class RowMismatch:
    frame_seq: int
    position: int
    event_seq: int
    differences: tuple[FieldDifference, ...]


@dataclass(frozen=True, slots=True)
class RowCountMismatch:
    """One frame's projection and one frame's rows are different lengths — a row gained or lost."""

    frame_seq: int
    projected: tuple[StoredEventKind, ...]
    stored: tuple[StoredEventKind, ...]


@dataclass(frozen=True, slots=True)
class RowsBeyondCursor:
    """Rows whose frame the cursor says was never projected.

    The write path commits the rows and the cursor in one transaction, so this is those two
    contradicting each other rather than the projection drifting.
    """

    frame_seq: int
    stored: tuple[StoredEventKind, ...]


@dataclass(frozen=True, slots=True)
class UnalignableRow:
    """A row with no frame range to align by — the `authored` arm, naming a turn.

    The console's own session events take that arm and are written turn-less
    (`session_events.authored`), so the per-turn read below does not see one and re-projection
    cannot delete what it never re-derives. This finding is for an authored row that *does* name a
    turn, which nothing writes today.
    """

    event_seq: int
    kind: StoredEventKind


type Finding = RowMismatch | RowCountMismatch | RowsBeyondCursor | UnalignableRow


class SkipReason(StrEnum):
    """An era the check cannot speak about, as opposed to a turn that agrees or drifts."""

    # `sessions.projected_frame_seq` is NULL or behind the turn (#4178's era), so nothing in it
    # claims to have been projected and re-folding would compare against a position no writer took.
    CURSOR_NEVER_REACHED = "cursor_never_reached"


@dataclass(frozen=True, slots=True)
class Agrees:
    """Every compared frame projects to exactly the rows that are stored for it."""


@dataclass(frozen=True, slots=True)
class Drifted:
    findings: tuple[Finding, ...]


@dataclass(frozen=True, slots=True)
class Skipped:
    reason: SkipReason


type Outcome = Agrees | Drifted | Skipped


@dataclass(frozen=True, slots=True)
class TurnReport:
    """One turn's outcome, plus the coverage that says how much of the turn it speaks for.

    A caller writing anything from an `Agrees` must read the coverage too: a turn with frames past
    the cursor is one adoption will still project, so its rows are not yet the whole turn.
    """

    turn_id: UUID
    first_frame_seq: int
    last_frame_seq: int | None
    outcome: Outcome
    # Recorded frames of this turn at or below the cursor: what the check actually compared.
    checked_frames: int
    stored_rows: int
    # Frames of this turn past the cursor: recorded, and their effects never committed.
    unprojected_frames: int


@dataclass(frozen=True, slots=True)
class SessionReport:
    session_id: UUID
    projected_frame_seq: int | None
    turns: tuple[TurnReport, ...]


async def check_session(db: AsyncSession, session_id: UUID) -> SessionReport:
    """Every turn of one session, re-projected and aligned against its rows."""
    chat = await db.get(Session, session_id)
    if chat is None:
        raise KeyError(session_id)
    turns = (
        await db.scalars(
            select(SessionTurn).where(SessionTurn.session_id == session_id).order_by(SessionTurn.first_frame_seq)
        )
    ).all()
    # An open turn has no upper bound of its own, so the next turn's lower bound stands in for one.
    ends_before = [*(turn.first_frame_seq for turn in turns[1:]), None]
    return SessionReport(
        session_id=session_id,
        projected_frame_seq=chat.projected_frame_seq,
        turns=tuple(
            [
                await _check_turn(db, turn, cursor=chat.projected_frame_seq, ends_before=upper)
                for turn, upper in zip(turns, ends_before, strict=True)
            ]
        ),
    )


async def _check_turn(
    db: AsyncSession, turn: SessionTurn, *, cursor: int | None, ends_before: int | None
) -> TurnReport:
    frames = await _turn_frames(db, turn, ends_before=ends_before)
    rows = (
        await db.scalars(
            select(SessionEvent).where(SessionEvent.turn_id == turn.turn_id).order_by(SessionEvent.event_seq)
        )
    ).all()
    projected = {frame.frame_seq: _expected(turn.turn_id, frame) for frame in frames}
    within = [seq for seq in projected if cursor is not None and seq <= cursor]
    return TurnReport(
        turn_id=turn.turn_id,
        first_frame_seq=turn.first_frame_seq,
        last_frame_seq=turn.last_frame_seq,
        outcome=_outcome(projected, within=within, rows=rows, cursor=cursor),
        checked_frames=len(within),
        stored_rows=len(rows),
        unprojected_frames=len(projected) - len(within),
    )


def _outcome(
    projected: dict[int, tuple[SessionEvent, ...]],
    *,
    within: Sequence[int],
    rows: Sequence[SessionEvent],
    cursor: int | None,
) -> Outcome:
    """One turn's outcome: the skip first, because an era is not a disagreement."""
    if not within:
        return Skipped(reason=SkipReason.CURSOR_NEVER_REACHED)
    findings: list[Finding] = [
        UnalignableRow(event_seq=row.event_seq, kind=row.kind)
        for row in rows
        if row.provenance is EventProvenance.AUTHORED or row.source_first_frame_seq is None
    ]
    stored: defaultdict[int, list[SessionEvent]] = defaultdict(list)
    for row in rows:
        if row.source_first_frame_seq is not None:
            stored[row.source_first_frame_seq].append(row)
    for frame_seq in sorted(set(projected) | set(stored)):
        kept = tuple(stored.get(frame_seq, ()))
        if cursor is None or frame_seq > cursor:
            if kept:
                findings.append(RowsBeyondCursor(frame_seq=frame_seq, stored=_kinds(kept)))
            continue
        findings.extend(_aligned(frame_seq, projected.get(frame_seq, ()), kept))
    return Drifted(findings=tuple(findings)) if findings else Agrees()


def _aligned(frame_seq: int, projected: Sequence[SessionEvent], stored: Sequence[SessionEvent]) -> list[Finding]:
    """One frame's rows against one frame's projection, in order.

    Position is the alignment, because both sequences come out of the same fold over the same frame
    — so a difference in length is a row gained or lost rather than a reordering to search for, and
    saying which is which is the reader's job and not this function's.
    """
    if len(projected) != len(stored):
        return [RowCountMismatch(frame_seq=frame_seq, projected=_kinds(projected), stored=_kinds(stored))]
    return [
        RowMismatch(frame_seq=frame_seq, position=position, event_seq=theirs.event_seq, differences=tuple(differences))
        for position, (mine, theirs) in enumerate(zip(projected, stored, strict=True))
        if (differences := _differences(mine, theirs))
    ]


def _differences(projected: SessionEvent, stored: SessionEvent) -> list[FieldDifference]:
    return [
        FieldDifference(field=field, projected=str(mine), stored=str(theirs))
        for field in ("kind", "provenance", "source_first_frame_seq", "source_last_frame_seq", "call_id", "body")
        if (mine := getattr(projected, field)) != (theirs := getattr(stored, field))
    ]


def _expected(turn_id: UUID, frame: SessionFrame) -> tuple[SessionEvent, ...]:
    """The rows one recorded frame would be written as, through the writer's own two functions."""
    return tuple(
        row
        for event in frame_projection.projected(frame_seq=frame.frame_seq, payload=frame.payload)
        if (row := session_events.row(event, session_id=frame.session_id, turn_id=turn_id, now=_UNCOMPARED_CLOCK))
        is not None
    )


def foldable_frames(session_id: UUID) -> Select[tuple[SessionFrame]]:
    """One session's recorded frames in order, less what the console authored.

    `setup_output` carries no protocol `type` for the fold to read, and a `partial` row was the
    console's own reconstruction of an answer in flight — the same exclusions adoption replays
    under (`session_store._unprojected_frames`), and `partial` outlives its last writer only until
    the column goes. Returned as a query so a caller can bound it further.
    """
    return (
        select(SessionFrame)
        .where(
            SessionFrame.session_id == session_id,
            SessionFrame.partial.is_(False),
            SessionFrame.kind != SETUP_OUTPUT_KIND,
        )
        .order_by(SessionFrame.frame_seq)
    )


async def _turn_frames(db: AsyncSession, turn: SessionTurn, *, ends_before: int | None) -> Sequence[SessionFrame]:
    query = foldable_frames(turn.session_id).where(SessionFrame.frame_seq >= turn.first_frame_seq)
    if (upper := turn.last_frame_seq) is not None:
        query = query.where(SessionFrame.frame_seq <= upper)
    elif ends_before is not None:
        query = query.where(SessionFrame.frame_seq < ends_before)
    return (await db.scalars(query)).all()


def _kinds(rows: Sequence[SessionEvent]) -> tuple[StoredEventKind, ...]:
    return tuple(row.kind for row in rows)
