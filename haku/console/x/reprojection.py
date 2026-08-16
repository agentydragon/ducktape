"""Re-project a recorded session's frames and say where `session_events` disagrees.

The determinism <../../plans/chat_runtime_projection.md> § "What makes it safe" rests on, checked
rather than asserted: the same frames project to the same events, so re-folding a stored session
has to reproduce its stored rows exactly, and everywhere it does not is either drift or a
projection bug worth repairing.

**The fold is the write path's own** (`frame_projection.projected`) and the row is built by the
write path's own mapping (`session_events.row`), so the only thing added here is the alignment.
That alignment is **by frame**: a frame's rows are written in one transaction and every event the
write path produces carries `(frame_seq, frame_seq)` as its range, so which rows a frame owns is a
lookup rather than a guess.

**Two bounds keep an honest report from being a false one:**

- **The cursor.** `sessions.projected_frame_seq` is how far the fold committed, so a frame past it
  is one whose effects were never written — a replica that died mid-turn — and is counted rather
  than reported.
- **The release.** A turn served before `session_events` had a writer has frames and no rows,
  which is indistinguishable from a projection that has stopped producing. Such a turn is skipped
  with its reason, per turn, so the check does not report drift on every live session for one
  `session_ttl_seconds` after the release.

The third case is genuinely ambiguous and is reported rather than guessed at: a turn whose rows
begin part-way through it, which is what a replica on the new image adopting a turn the old one
started produces — and also what a projection that dropped its first frames produces.
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

from haku.console.chat_models import ConversationEventKind, EventProvenance
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
    and a JSON body, and the only consumer is a report a person reads.
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
    projected: tuple[ConversationEventKind, ...]
    stored: tuple[ConversationEventKind, ...]


@dataclass(frozen=True, slots=True)
class RowsBeyondCursor:
    """Rows whose frame the cursor says was never projected.

    The write path commits the rows and the cursor in one transaction, so this is those two
    contradicting each other rather than the projection drifting.
    """

    frame_seq: int
    stored: tuple[ConversationEventKind, ...]


@dataclass(frozen=True, slots=True)
class UnalignableRow:
    """A row with no frame range to align by — the `authored` arm, which no writer produces yet."""

    event_seq: int
    kind: ConversationEventKind


type Finding = RowMismatch | RowCountMismatch | RowsBeyondCursor | UnalignableRow


class Verdict(StrEnum):
    AGREES = "agrees"
    DRIFTED = "drifted"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class TurnReport:
    turn_id: UUID
    first_frame_seq: int
    last_frame_seq: int | None
    verdict: Verdict
    # Recorded frames of this turn at or below the cursor: what the check actually covered.
    checked_frames: int
    stored_rows: int
    # Frames of this turn past the cursor: recorded, and their effects never committed.
    unprojected_frames: int
    # Why a skipped turn was skipped; None on every other verdict.
    skipped_because: str | None
    findings: tuple[Finding, ...]


@dataclass(frozen=True, slots=True)
class SessionReport:
    session_id: UUID
    projected_frame_seq: int | None
    turns: tuple[TurnReport, ...]

    @property
    def drifted(self) -> tuple[TurnReport, ...]:
        return tuple(turn for turn in self.turns if turn.verdict is Verdict.DRIFTED)


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


async def recent_sessions(db: AsyncSession, *, limit: int) -> Sequence[UUID]:
    """The most recently created sessions that ran at least one turn — the tool's default range."""
    return (
        await db.scalars(
            select(Session.session_id)
            .where(Session.session_id.in_(select(SessionTurn.session_id)))
            .order_by(Session.created_at.desc())
            .limit(limit)
        )
    ).all()


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
    verdict, skipped_because, findings = _aligned_turn(projected, within=within, rows=rows, cursor=cursor)
    return TurnReport(
        turn_id=turn.turn_id,
        first_frame_seq=turn.first_frame_seq,
        last_frame_seq=turn.last_frame_seq,
        verdict=verdict,
        checked_frames=len(within),
        stored_rows=len(rows),
        unprojected_frames=len(projected) - len(within),
        skipped_because=skipped_because,
        findings=findings,
    )


def _aligned_turn(
    projected: dict[int, tuple[SessionEvent, ...]],
    *,
    within: Sequence[int],
    rows: Sequence[SessionEvent],
    cursor: int | None,
) -> tuple[Verdict, str | None, tuple[Finding, ...]]:
    """One turn's verdict: the two skips first, because both are eras rather than disagreements."""
    if not within:
        # The cursor's own era (#4178): a session whose cursor is NULL or behind this turn was
        # served by a replica that did not advance it, so nothing here is claimed to be projected.
        return Verdict.SKIPPED, "the cursor names no frame of this turn, so nothing here claims to be projected", ()
    if not rows and any(projected[seq] for seq in within):
        # A turn served by a replica whose image had no writer for these rows looks exactly like
        # one whose projection stopped producing, and no column tells them apart.
        # CLEANUP(added 2026-08-16): delete this arm once no session that can still acquire a
        #   frame predates the release that writes `session_events` — `session_ttl_seconds` (7200)
        #   clears them within two hours of it converging:
        #     SELECT count(*) FROM session_turns t
        #      WHERE NOT EXISTS (SELECT 1 FROM session_events e WHERE e.turn_id = t.turn_id)
        #        AND t.started_at > now() - interval '2 hours';
        #   After that, a turn with frames and no rows is drift and must be reported as one.
        return (
            Verdict.SKIPPED,
            "no rows at all: served before the release that writes them, or a projection that stopped",
            (),
        )
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
    return (Verdict.DRIFTED if findings else Verdict.AGREES), None, tuple(findings)


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
    """One session's recorded frames in order, less the two rows the console authored.

    `setup_output` carries no protocol `type` for the fold to read and a `partial` row is the
    console's own reconstruction of an answer in flight — the same exclusions adoption replays
    under (`session_store._unprojected_frames`). Returned as a query so a caller can bound it
    further; `message_provenance` re-projects the same frames to recover a message's own range.
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


def _kinds(rows: Sequence[SessionEvent]) -> tuple[ConversationEventKind, ...]:
    return tuple(row.kind for row in rows)


def rendered(report: SessionReport) -> list[str]:
    """The report as lines: one per session, one per turn, one per finding."""
    counts = {verdict: sum(1 for turn in report.turns if turn.verdict is verdict) for verdict in Verdict}
    summary = ", ".join(f"{count} {verdict}" for verdict, count in counts.items() if count)
    lines = [
        f"session {report.session_id} cursor={report.projected_frame_seq} "
        f"{len(report.turns)} turn(s): {summary or 'no turns'}"
    ]
    for turn in report.turns:
        upper = turn.last_frame_seq if turn.last_frame_seq is not None else "open"
        detail = f" — {turn.skipped_because}" if turn.skipped_because is not None else ""
        lines.append(
            f"  turn {turn.turn_id} frames {turn.first_frame_seq}-{upper} checked={turn.checked_frames} "
            f"rows={turn.stored_rows} unprojected={turn.unprojected_frames} {turn.verdict}{detail}"
        )
        lines.extend(f"    {_rendered_finding(finding)}" for finding in turn.findings)
    return lines


def _rendered_finding(finding: Finding) -> str:
    match finding:
        case RowMismatch():
            differences = "; ".join(
                f"{difference.field}: projected={_clipped(difference.projected)} stored={_clipped(difference.stored)}"
                for difference in finding.differences
            )
            return f"frame {finding.frame_seq} row {finding.position} (event_seq {finding.event_seq}): {differences}"
        case RowCountMismatch():
            return (
                f"frame {finding.frame_seq}: projects to {_listed(finding.projected)}, stored {_listed(finding.stored)}"
            )
        case RowsBeyondCursor():
            return f"frame {finding.frame_seq}: stored {_listed(finding.stored)} past the cursor"
        case UnalignableRow():
            return f"event_seq {finding.event_seq} ({finding.kind}): authored, so no frame to align it by"


def _listed(kinds: tuple[ConversationEventKind, ...]) -> str:
    return ",".join(kinds) or "nothing"


def _clipped(value: str, *, width: int = 200) -> str:
    return value if len(value) <= width else f"{value[:width]}…"
