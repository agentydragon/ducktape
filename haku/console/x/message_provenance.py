"""Recover the frame range of a message row that migration `0045` could not point.

`session_messages.source_{first,last}_frame_seq` is how a message finds its tool calls now that
`rollout_calls` is gone (`database_schema.SessionEvent`). `0045` filled it wherever the row's
`agent_message_id` named an `assistant` frame; the rows with no agent id — the population the
neutral message exists to fix — were left NULL, and there is nothing on such a row to join by.

**So the pointer is recovered by re-projecting, and the alignment is the prose.** The session's
frames are folded exactly as the write path folds them (`frame_projection.projected`), and the
message rows that fold *would have written* are reconstructed from it: where each one opened, where
it closed, and what it ended up saying. A stored row is then matched to one of those candidates by
the text both carry. Nothing else on an unpointed row survives to key on — it has no agent id, no
turn, and its timestamps come from a clock the frames do not share.

**Ambiguity is counted, never guessed.** Identical prose is common (a turn's `result` repeats its
last message, and "Done." is said often), so the rule is one of arithmetic rather than preference:
the unpointed rows carrying a text and the unclaimed candidates carrying it are paired **in order**
only when there are equally many, and a candidate a pointed row already claims is not available to
be paired at all. Anything else — no candidate, or a different number of them — leaves the row
unfilled with `MessageUnpointable` saying which. A last pass rejects a match that would place a row
before one pointed earlier in the transcript, since messages are written in frame order.

**Reading and writing are separate calls.** `plan` computes and writes nothing; `apply` takes what
`plan` returned. A dry run is therefore not a mode but the absence of the second call.

**Sessions with an open turn are left alone.** Their rows are the runtime's to point, it is still
projecting frames into them, and `ck_session_messages_unpointable_exclusive` would turn a reason
written under a live turn into a rejected write in that turn.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from haku.console.chat_models import ChatMessageRole, FrameDirection, MessageUnpointable
from haku.console.database_schema import SessionFrame, SessionMessage, SessionTurn
from haku.console.x import reprojection
from haku.console.x.claude_code.frames import PROMPT_FRAME_KIND
from haku.console.x.conversation_events import MessageCompleted, TextDelta, TurnCompleted
from haku.console.x.frame_projection import projected


@dataclass(frozen=True, slots=True)
class Candidate:
    """One message the frames say was written, and the inclusive range it was built from."""

    first_frame_seq: int
    last_frame_seq: int
    text: str


@dataclass(frozen=True, slots=True)
class Fill:
    message_id: UUID
    first_frame_seq: int
    last_frame_seq: int


@dataclass(frozen=True, slots=True)
class Unfillable:
    message_id: UUID
    reason: MessageUnpointable


type Outcome = Fill | Unfillable


@dataclass(frozen=True, slots=True)
class SessionPlan:
    """What one session's unpointed rows would become. Empty on both counts when it has none."""

    session_id: UUID
    fills: tuple[Fill, ...]
    unfillable: tuple[Unfillable, ...]

    @property
    def reasons(self) -> dict[MessageUnpointable, int]:
        return {
            reason: sum(1 for row in self.unfillable if row.reason is reason)
            for reason in MessageUnpointable
            if any(row.reason is reason for row in self.unfillable)
        }


async def unpointed_sessions(db: AsyncSession, *, limit: int) -> Sequence[UUID]:
    """Sessions holding an unpointed, unexplained message row and no turn still running."""
    return (
        await db.scalars(
            select(SessionMessage.session_id)
            .where(SessionMessage.source_first_frame_seq.is_(None), SessionMessage.unpointable_reason.is_(None))
            .where(
                ~select(SessionTurn.turn_id)
                .where(SessionTurn.session_id == SessionMessage.session_id, SessionTurn.ended_at.is_(None))
                .exists()
            )
            .group_by(SessionMessage.session_id)
            .order_by(SessionMessage.session_id)
            .limit(limit)
        )
    ).all()


async def plan(db: AsyncSession, session_id: UUID) -> SessionPlan:
    """What one session's frames say about the rows in it that carry no range. Writes nothing."""
    frames = (await db.scalars(reprojection.foldable_frames(session_id))).all()
    rows = (
        await db.scalars(
            select(SessionMessage)
            .where(SessionMessage.session_id == session_id, SessionMessage.unpointable_reason.is_(None))
            .order_by(SessionMessage.created_at, SessionMessage.message_id)
        )
    ).all()
    outcomes = [
        *_aligned(
            [row for row in rows if row.role is ChatMessageRole.ASSISTANT],
            _projected_messages([frame for frame in frames if frame.direction is FrameDirection.FROM_AGENT]),
        ),
        *_aligned([row for row in rows if row.role is ChatMessageRole.USER], _prompts(frames)),
    ]
    return SessionPlan(
        session_id=session_id,
        fills=tuple(outcome for outcome in outcomes if isinstance(outcome, Fill)),
        unfillable=tuple(outcome for outcome in outcomes if isinstance(outcome, Unfillable)),
    )


async def apply(db: AsyncSession, session: SessionPlan) -> None:
    """Write one session's plan. The caller commits, so a session lands whole or not at all."""
    for fill in session.fills:
        await db.execute(
            update(SessionMessage)
            .where(SessionMessage.message_id == fill.message_id)
            .values(source_first_frame_seq=fill.first_frame_seq, source_last_frame_seq=fill.last_frame_seq)
        )
    for row in session.unfillable:
        await db.execute(
            update(SessionMessage)
            .where(SessionMessage.message_id == row.message_id)
            .values(unpointable_reason=row.reason)
        )


@dataclass(slots=True)
class _OpenMessage:
    """A message the fold has opened and not yet closed, as `apply_frame` holds one."""

    first_frame_seq: int
    last_frame_seq: int
    streamed: list[str] = field(default_factory=list)


def _projected_messages(frames: Sequence[SessionFrame]) -> list[Candidate]:
    """The message rows re-projecting these frames would write, in order.

    `apply_frame`'s own bookkeeping, less the writing: a delta opens the row and moves its far end,
    a completed message closes it at its own frame, and a turn whose frames completed no message at
    all leaves `_run_turn` minting one row whose only source is the `result` frame. That last text
    is read off the payload rather than off `TurnCompleted`, which carries an outcome and no prose
    — the same appeal to the frame `_run_turn` makes for it.
    """
    candidates: list[Candidate] = []
    open_message: _OpenMessage | None = None
    said_anything = False
    for frame in frames:
        for event in projected(frame_seq=frame.frame_seq, payload=frame.payload):
            match event:
                case TextDelta():
                    if open_message is None:
                        open_message = _OpenMessage(frame.frame_seq, frame.frame_seq)
                    open_message.last_frame_seq = frame.frame_seq
                    open_message.streamed.append(event.text)
                case MessageCompleted():
                    opened = open_message.first_frame_seq if open_message is not None else frame.frame_seq
                    streamed = "" if open_message is None else "".join(open_message.streamed)
                    candidates.append(
                        Candidate(opened, frame.frame_seq, (event.text or "").strip() or streamed.strip())
                    )
                    open_message, said_anything = None, True
                case TurnCompleted():
                    if open_message is not None:
                        candidates.append(
                            Candidate(
                                open_message.first_frame_seq,
                                open_message.last_frame_seq,
                                "".join(open_message.streamed).strip(),
                            )
                        )
                    elif not said_anything:
                        candidates.append(Candidate(frame.frame_seq, frame.frame_seq, _result_text(frame.payload)))
                    open_message, said_anything = None, False
                case _:
                    pass
    return candidates


def _result_text(payload: dict[str, Any]) -> str:
    return str(payload.get("result") or "").strip()


def _prompts(frames: Sequence[SessionFrame]) -> list[Candidate]:
    """The operator's own questions, as the frames they went out as.

    A prompt's row exists before its frame does — the operator typed it, and only `client.query`
    knows where it landed — so a row written before `set_message_source_frames` has no pointer for
    the same reason an assistant row from that era has none. A `user` frame going the other way
    carries a tool result, whose content is a list rather than a string.
    """
    return [
        Candidate(frame.frame_seq, frame.frame_seq, content.strip())
        for frame in frames
        if frame.direction is FrameDirection.TO_AGENT
        and frame.kind == PROMPT_FRAME_KIND
        and isinstance(message := frame.payload.get("message"), dict)
        and isinstance(content := message.get("content"), str)
    ]


def _aligned(rows: Sequence[SessionMessage], candidates: Sequence[Candidate]) -> Iterator[Outcome]:
    """One role's rows in transcript order against the candidates its frames produce."""
    claimed = {(row.source_first_frame_seq, row.source_last_frame_seq) for row in rows}
    unclaimed: defaultdict[str, list[Candidate]] = defaultdict(list)
    for candidate in candidates:
        if (candidate.first_frame_seq, candidate.last_frame_seq) not in claimed:
            unclaimed[candidate.text].append(candidate)
    by_text: defaultdict[str, list[SessionMessage]] = defaultdict(list)
    for row in rows:
        if row.source_first_frame_seq is None:
            by_text[row.content.strip()].append(row)

    proposed: dict[UUID, Candidate] = {}
    refused: dict[UUID, MessageUnpointable] = {}
    for text, group in by_text.items():
        match unclaimed.get(text, []):
            case []:
                refused |= {row.message_id: MessageUnpointable.NO_MATCHING_PROJECTION for row in group}
            case matches if len(matches) == len(group):
                proposed |= {row.message_id: chosen for row, chosen in zip(group, matches, strict=True)}
            case _:
                refused |= {row.message_id: MessageUnpointable.AMBIGUOUS_TEXT for row in group}

    # The transcript's frame order so far, pointed rows included: a match below it is a match to
    # the wrong message, whatever its prose says.
    floor = 0
    for row in rows:
        if row.source_first_frame_seq is not None:
            floor = max(floor, row.source_first_frame_seq)
        elif (chosen := proposed.get(row.message_id)) is None:
            yield Unfillable(row.message_id, refused[row.message_id])
        elif chosen.first_frame_seq < floor:
            yield Unfillable(row.message_id, MessageUnpointable.OUT_OF_ORDER)
        else:
            floor = chosen.first_frame_seq
            yield Fill(row.message_id, chosen.first_frame_seq, chosen.last_frame_seq)


def rendered(sessions: Sequence[SessionPlan]) -> list[str]:
    """The scan as lines: one per session that had anything to say, then the totals."""
    lines = [
        f"session {session.session_id}: {len(session.fills)} filled, {len(session.unfillable)} unfillable"
        + ("" if not session.unfillable else " — " + _counted(session.reasons))
        for session in sessions
        if session.fills or session.unfillable
    ]
    filled = sum(len(session.fills) for session in sessions)
    totals: dict[MessageUnpointable, int] = defaultdict(int)
    for session in sessions:
        for reason, count in session.reasons.items():
            totals[reason] += count
    return [
        *lines,
        f"{len(sessions)} session(s): {filled} filled, {sum(totals.values())} unfillable"
        + ("" if not totals else " — " + _counted(totals)),
    ]


def _counted(reasons: dict[MessageUnpointable, int]) -> str:
    return ", ".join(f"{count} {reason}" for reason, count in sorted(reasons.items()))
