"""Re-project a recorded session's frames and say where the conversation log disagrees.

`check_session` returns findings; it prints nothing and decides nothing, so a caller chooses what a
per-turn disagreement is worth.

**The fold is the selected integration's live turn handler** and each event is spelled by
the write path's own mapping (`conversation_events.stored`), so the only thing added here is the
alignment. That alignment is **by the first frame in the event's provenance**. Most events name one
frame; a message completion and a streamed tool declaration can span several, and the stored row
keeps that same inclusive range. Cursor coverage is still decided by the frame whose fold emitted
the event: a later frame can close an item whose own last content frame came before it.

**What is compared is the kind and the body, not the whole row.** `item_id` is minted and
`event_seq` allocated as rows are written, so a re-projection cannot reproduce either and does not
claim to. What it can say is that the same frames still mean the same things.

**And separately, that the items agree with the log.** `conversation_item.text` is a
materialisation of the segments and never a second authority for them
(<../docs/conversation_schema.md> § 2), so folding the segments back and comparing is the check the
whole shape exists to make possible — the one the old transcript table could not be given, because
its rows and the log were written from different places.

**One bound keeps an honest report from being a false one: the cursor.**
`sessions.projected_frame_seq` is how far the fold committed, so a frame past it is one whose
effects were never written — a replica that died mid-turn — and is counted rather than reported.

The ambiguous case is reported rather than guessed at: a turn whose rows begin part-way through it,
which a replica adopting a turn another started produces, and so does a projection that dropped its
first frames.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import Select, func, literal_column, select
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.ext.asyncio import AsyncSession

from haku.console.conversation.conversation_event import (
    ConversationEventKind,
    EventProvenance,
    FrameRange,
    StoredEventKind,
)
from haku.console.database_schema import (
    Conversation,
    ConversationEventRow,
    ConversationItem,
    ConversationTurn,
    Session,
    SessionFrame,
)
from haku.console.harnesses.kind import HarnessKind
from haku.console.session.session_frames import BridgeFrameKind
from haku.console.x import conversation_events
from haku.console.x.runtime import RuntimeRegistry
from haku.runtime.x.bridge.protocol import HarnessFrame
from util.enum_vocab import UnknownValue


@dataclass(frozen=True, slots=True)
class ProjectedRow:
    """What one event of one frame would be stored as, less what only a writer can assign."""

    kind: StoredEventKind
    body: dict[str, Any]
    source_first_frame_seq: int
    source_last_frame_seq: int
    # The frame whose fold emitted this row. Distinct from provenance: a new message frame can
    # close prose whose own last contributing frame came before it.
    emitted_at_frame_seq: int


@dataclass(frozen=True, slots=True)
class FieldDifference:
    """One column of one row, as the frames project it and as it is stored.

    Rendered to text rather than carried typed: the field set spans an enum, two integers, a string
    and a JSON body, and a caller acts on *that* a column differs.
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
    projected: tuple[StoredEventKind | UnknownValue, ...]
    stored: tuple[StoredEventKind | UnknownValue, ...]


@dataclass(frozen=True, slots=True)
class RowsBeyondCursor:
    """Rows whose frame the cursor says was never projected.

    The write path commits the rows and the cursor in one transaction, so this is those two
    contradicting each other rather than the projection drifting.
    """

    frame_seq: int
    stored: tuple[StoredEventKind | UnknownValue, ...]


@dataclass(frozen=True, slots=True)
class ItemTextMismatch:
    """An item's `text` is not the concatenation of its segments.

    The invariant the whole shape exists for: prose lives in segments and the column is their fold,
    so a disagreement means the materialisation drifted from the only thing that is supposed to
    produce it.
    """

    item_id: UUID
    folded: str
    stored: str


type Finding = RowMismatch | RowCountMismatch | RowsBeyondCursor


class SkipReason(StrEnum):
    """An era the check cannot speak about, as opposed to a turn that agrees or drifts."""

    # `sessions.projected_frame_seq` is NULL or behind the turn, so nothing in it claims to have
    # been projected and re-folding would compare against a position no writer took.
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
    # Absent on a turn that opened and recorded no frame: the CLI died before answering.
    first_frame_seq: int | None
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
    # Items of this session whose text disagrees with their own segments. Session-wide rather than
    # per turn, because a prompt item belongs to the conversation before any turn claims it.
    items: tuple[ItemTextMismatch, ...]


async def check_session(db: AsyncSession, session_id: UUID, *, runtimes: RuntimeRegistry) -> SessionReport:
    """Every turn of one session, re-projected and aligned against its rows."""
    registry = runtimes
    row = (
        await db.execute(
            select(Session, Conversation.runtime_kind)
            .join(Conversation, Conversation.conversation_id == Session.conversation_id)
            .where(Session.session_id == session_id)
        )
    ).one_or_none()
    if row is None:
        raise KeyError(session_id)
    chat, runtime_kind = row
    turns = (
        await db.scalars(
            select(ConversationTurn)
            .where(ConversationTurn.session_id == session_id)
            .order_by(ConversationTurn.first_seq)
        )
    ).all()
    # An open turn has no upper bound of its own, so the next turn's lower bound stands in for one.
    ends_before = [*(turn.first_frame_seq for turn in turns[1:]), None]
    return SessionReport(
        session_id=session_id,
        projected_frame_seq=chat.projected_frame_seq,
        turns=tuple(
            [
                await _check_turn(
                    db,
                    turn,
                    cursor=chat.projected_frame_seq,
                    ends_before=upper,
                    runtime_kind=runtime_kind,
                    runtimes=registry,
                )
                for turn, upper in zip(turns, ends_before, strict=True)
            ]
        ),
        items=await check_item_text(db, session_id),
    )


async def check_item_text(db: AsyncSession, session_id: UUID) -> tuple[ItemTextMismatch, ...]:
    """One session's items whose `text` is not what their segments concatenate to.

    Folded in Postgres rather than read back row by row: a long session's segments run to thousands
    of rows and the check is meant to be cheap enough to run over a whole session.
    """
    folded = (
        select(
            ConversationEventRow.item_id.label("item_id"),
            func.string_agg(
                ConversationEventRow.body["text"].astext,
                aggregate_order_by(literal_column("''"), ConversationEventRow.event_seq),
            ).label("text"),
        )
        .where(ConversationEventRow.kind == ConversationEventKind.ITEM_SEGMENT)
        .group_by(ConversationEventRow.item_id)
        .subquery()
    )
    rows = await db.execute(
        select(ConversationItem.item_id, ConversationItem.item_text, func.coalesce(folded.c.text, ""))
        .outerjoin(folded, folded.c.item_id == ConversationItem.item_id)
        .where(
            ConversationItem.session_id == session_id, ConversationItem.item_text != func.coalesce(folded.c.text, "")
        )
        .order_by(ConversationItem.opened_seq)
    )
    return tuple(
        ItemTextMismatch(item_id=item_id, folded=folded_text, stored=stored) for item_id, stored, folded_text in rows
    )


async def _check_turn(
    db: AsyncSession,
    turn: ConversationTurn,
    *,
    cursor: int | None,
    ends_before: int | None,
    runtime_kind: HarnessKind,
    runtimes: RuntimeRegistry,
) -> TurnReport:
    frames = await _turn_frames(db, turn, ends_before=ends_before)
    # The fold's own output and nothing else. An authored row may name a turn (`turn_started` and
    # `turn_ended` both do) and re-projecting frames can never re-derive one, so comparing it
    # against the fold would report drift on every turn.
    rows = (
        await db.scalars(
            select(ConversationEventRow)
            .where(
                ConversationEventRow.turn_id == turn.turn_id,
                ConversationEventRow.provenance == EventProvenance.FRAME_RANGE,
            )
            .order_by(ConversationEventRow.event_seq)
        )
    ).all()
    projected = _expected(frames, runtime_kind=runtime_kind, runtimes=runtimes)
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
    projected: dict[int, tuple[ProjectedRow, ...]],
    *,
    within: Sequence[int],
    rows: Sequence[ConversationEventRow],
    cursor: int | None,
) -> Outcome:
    """One turn's outcome: the skip first, because an era is not a disagreement."""
    if not within:
        return Skipped(reason=SkipReason.CURSOR_NEVER_REACHED)
    assert cursor is not None
    findings: list[Finding] = []
    stored: defaultdict[int, list[ConversationEventRow]] = defaultdict(list)
    for row in rows:
        # `ck_conversation_event_provenance_frames` puts a range on exactly the arm this selects.
        assert row.source_first_frame_seq is not None
        stored[row.source_first_frame_seq].append(row)
    for frame_seq in sorted(set(projected) | set(stored)):
        projected_here = tuple(row for row in projected.get(frame_seq, ()) if row.emitted_at_frame_seq <= cursor)
        stored_here = tuple(
            row
            for row in stored.get(frame_seq, ())
            if row.source_last_frame_seq is not None and row.source_last_frame_seq <= cursor
        )
        beyond = tuple(
            row
            for row in stored.get(frame_seq, ())
            if row.source_last_frame_seq is not None and row.source_last_frame_seq > cursor
        )
        if beyond:
            findings.append(RowsBeyondCursor(frame_seq=frame_seq, stored=_kinds(beyond)))
        if frame_seq > cursor:
            continue
        findings.extend(_aligned(frame_seq, projected_here, stored_here))
    return Drifted(findings=tuple(findings)) if findings else Agrees()


def _aligned(
    frame_seq: int, projected: Sequence[ProjectedRow], stored: Sequence[ConversationEventRow]
) -> list[Finding]:
    """One frame's rows against one frame's projection, in order.

    Position is the alignment, because both sequences come out of the same fold over the same
    frame, so a difference in length is a row gained or lost rather than a reordering to search for.
    """
    if len(projected) != len(stored):
        return [RowCountMismatch(frame_seq=frame_seq, projected=_kinds(projected), stored=_kinds(stored))]
    return [
        RowMismatch(frame_seq=frame_seq, position=position, event_seq=theirs.event_seq, differences=tuple(differences))
        for position, (mine, theirs) in enumerate(zip(projected, stored, strict=True))
        if (differences := _differences(mine, theirs))
    ]


def _differences(projected: ProjectedRow, stored: ConversationEventRow) -> list[FieldDifference]:
    """The columns a re-projection can speak for, compared.

    The event carries the exact range it was read from. Comparing both ends matters for composed
    calls: the row is written only once the terminal stream frame makes its partial JSON complete.
    """
    expected = {
        "kind": projected.kind,
        "provenance": EventProvenance.FRAME_RANGE,
        "source_first_frame_seq": projected.source_first_frame_seq,
        "source_last_frame_seq": projected.source_last_frame_seq,
        "body": projected.body,
    }
    return [
        FieldDifference(field=field, projected=str(mine), stored=str(theirs))
        for field, mine in expected.items()
        if mine != (theirs := getattr(stored, field))
    ]


def _expected(
    frames: Sequence[SessionFrame], *, runtime_kind: HarnessKind, runtimes: RuntimeRegistry
) -> dict[int, tuple[ProjectedRow, ...]]:
    """What each of a turn's recorded frames would be written as, through the writer's own two
    functions — folded with one state across the turn, because that is how the writer folds it.
    """
    handler = runtimes[runtime_kind].turn_handler()
    said: defaultdict[int, list[ProjectedRow]] = defaultdict(list)
    for frame in frames:
        # A frame with no events still belongs in coverage and can have stored rows to disagree
        # with, so retain an empty bucket for it.
        said[frame.frame_seq]
        effects = handler.apply(
            frame_seq=frame.frame_seq, frame=HarnessFrame(frame=frame.payload, seq=frame.runner_seq)
        )
        for event in effects.events:
            if (row := conversation_events.stored(event)) is None:
                continue
            if not isinstance(provenance := event.provenance, FrameRange):
                raise AssertionError(f"a projected stored event names no frame range: {event=}")
            kind, body = row
            said[provenance.first_frame_seq].append(
                ProjectedRow(
                    kind=kind,
                    body=body.model_dump(mode="json"),
                    source_first_frame_seq=provenance.first_frame_seq,
                    source_last_frame_seq=provenance.last_frame_seq,
                    emitted_at_frame_seq=frame.frame_seq,
                )
            )
    return {frame_seq: tuple(rows) for frame_seq, rows in said.items()}


def foldable_frames(session_id: UUID) -> Select[tuple[SessionFrame]]:
    """One session's recorded frames in order, less what the console authored.

    `setup_output` carries no protocol `type` for the fold to read — the same exclusion adoption
    replays under (`session_store._unprojected_frames`). Returned as a query so a caller can bound
    it further.
    """
    return (
        select(SessionFrame)
        .where(SessionFrame.session_id == session_id, SessionFrame.kind == BridgeFrameKind.HARNESS_FRAME)
        .order_by(SessionFrame.frame_seq)
    )


async def _turn_frames(db: AsyncSession, turn: ConversationTurn, *, ends_before: int | None) -> Sequence[SessionFrame]:
    if turn.first_frame_seq is None:
        # A turn that opened and recorded no frame — the CLI died before answering. There is
        # nothing to re-project and nothing that claims to have been.
        return ()
    query = foldable_frames(turn.session_id).where(SessionFrame.frame_seq >= turn.first_frame_seq)
    if (upper := turn.last_frame_seq) is not None:
        query = query.where(SessionFrame.frame_seq <= upper)
    elif ends_before is not None:
        query = query.where(SessionFrame.frame_seq < ends_before)
    return (await db.scalars(query)).all()


def _kinds(rows: Sequence[ProjectedRow] | Sequence[ConversationEventRow]) -> tuple[StoredEventKind | UnknownValue, ...]:
    """A row of a kind this release has no words for is reported like any other difference.

    It is genuine drift from where this check stands: the fold here cannot have produced it, so
    saying so is the honest answer where raising would make the whole session unreportable.
    """
    return tuple(row.kind for row in rows)
