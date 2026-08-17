"""What `check_session` reports about sessions the write path itself produced.

The round trip is the point: frames recorded and projected exactly as the turn loop does it must
re-project to the rows that were written, so anything reported on such a session is a defect in the
comparison rather than in the projection.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest_bazel
from more_itertools import one
from sqlalchemy import delete, update

from haku.console.chat_models import ConversationEventKind, FrameDirection, TurnOutcome
from haku.console.database_schema import Session, SessionEvent
from haku.console.x import reprojection
from haku.console.x.frame_projection import projected
from haku.console.x.session_store import BridgeAuthentication, SpaSession


def _assistant(*blocks: dict[str, Any], message_id: str = "msg_1") -> dict[str, Any]:
    """One `assistant` frame. Every frame in a session needs its own *message_id*: `frame_uid`
    dedupes on it, so two frames sharing one are one frame to the recorder."""
    return {"type": "assistant", "message": {"id": message_id, "role": "assistant", "content": list(blocks)}}


def _tool_result(call_id: str, text: str) -> dict[str, Any]:
    return {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call_id, "content": text}]},
        "tool_use_result": {"exit_code": 0},
    }


async def _turn_through_the_write_path(chat_store, operator_id, frames: list[dict[str, Any]]) -> tuple[UUID, UUID]:
    """One session and one open turn, with *frames* recorded and projected as the turn loop does."""
    view, token = await chat_store.create(operator_id, SpaSession())
    session_id = view.session_id
    assert await chat_store.authenticate_bridge(session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, session_id, "what is in here?")
    started = await chat_store.next_prompt(session_id)
    assert started is not None
    for payload in frames:
        recorded = await chat_store.record_frame(session_id, FrameDirection.FROM_AGENT, payload["type"], payload)
        await chat_store.apply_frame(
            session_id, started.turn_id, recorded.frame_seq, projected(frame_seq=recorded.frame_seq, payload=payload)
        )
    return session_id, started.turn_id


async def test_a_session_the_write_path_projected_agrees_with_itself(
    chat_store, migrated_sessions, operator_id
) -> None:
    session_id, turn_id = await _turn_through_the_write_path(
        chat_store,
        operator_id,
        [
            _assistant({"type": "thinking", "thinking": "which files"}),
            _assistant(
                {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}}, message_id="msg_2"
            ),
            _tool_result("toolu_1", "a.py"),
            _assistant({"type": "text", "text": "one file"}, message_id="msg_3"),
        ],
    )

    async with migrated_sessions() as db:
        report = await reprojection.check_session(db, session_id)

    turn = one(report.turns)
    assert (turn.turn_id, turn.outcome) == (turn_id, reprojection.Agrees())
    # Two rows per `assistant` frame: what the block said, and the message the frame closed —
    # per-frame seeding means a message always ends at its own frame.
    assert turn.stored_rows == 6
    assert turn.unprojected_frames == 0


async def test_an_aborted_turn_still_agrees_with_its_frames(chat_store, migrated_sessions, operator_id) -> None:
    """The authored arm is out of scope, and `turn_aborted` is the first member of it to name a
    turn — so the per-turn read has to exclude it. Comparing it against a re-fold would report
    drift on every turn the operator stopped, since no frame projects to a row nothing sent.
    """
    session_id, turn_id = await _turn_through_the_write_path(
        chat_store, operator_id, [_assistant({"type": "text", "text": "one file"})]
    )
    await chat_store.end_turn(turn_id, TurnOutcome.ABORTED)

    async with migrated_sessions() as db:
        report = await reprojection.check_session(db, session_id)

    assert one(report.turns).outcome == reprojection.Agrees()


async def test_a_row_whose_body_was_edited_is_reported_against_its_frame(
    chat_store, migrated_sessions, operator_id
) -> None:
    """The comparison's whole purpose: a stored row the frames do not project to any more."""
    session_id, _ = await _turn_through_the_write_path(
        chat_store,
        operator_id,
        [_assistant({"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}})],
    )
    async with migrated_sessions() as db:
        await db.execute(
            update(SessionEvent)
            .where(SessionEvent.session_id == session_id, SessionEvent.kind == ConversationEventKind.TOOL_CALL_STARTED)
            .values(body={"tool_name": "Write", "arguments": {"command": "ls"}})
        )
        await db.commit()
        report = await reprojection.check_session(db, session_id)

    outcome = one(report.turns).outcome
    assert isinstance(outcome, reprojection.Drifted)
    mismatch = one(outcome.findings)
    assert isinstance(mismatch, reprojection.RowMismatch)
    assert [difference.field for difference in mismatch.differences] == ["body"]
    assert "Bash" in mismatch.differences[0].projected
    assert "Write" in mismatch.differences[0].stored


async def test_a_row_that_is_gone_is_a_count_mismatch_rather_than_a_silent_pass(
    chat_store, migrated_sessions, operator_id
) -> None:
    session_id, _ = await _turn_through_the_write_path(
        chat_store,
        operator_id,
        [
            _assistant(
                {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}},
                {"type": "text", "text": "looking"},
            )
        ],
    )
    async with migrated_sessions() as db:
        await db.execute(
            delete(SessionEvent).where(
                SessionEvent.session_id == session_id, SessionEvent.kind == ConversationEventKind.TOOL_CALL_STARTED
            )
        )
        await db.commit()
        report = await reprojection.check_session(db, session_id)

    outcome = one(report.turns).outcome
    assert isinstance(outcome, reprojection.Drifted)
    finding = one(outcome.findings)
    assert isinstance(finding, reprojection.RowCountMismatch)
    assert finding.projected == (ConversationEventKind.TOOL_CALL_STARTED, ConversationEventKind.MESSAGE_COMPLETED)
    assert finding.stored == (ConversationEventKind.MESSAGE_COMPLETED,)


async def test_a_turn_with_frames_and_no_rows_is_drift(chat_store, migrated_sessions, operator_id) -> None:
    """No writer leaves a projected frame without its rows, so a turn with none is the projection
    having stopped producing — reported against the frame that should have written them, exactly as
    a single missing row is."""
    session_id, _ = await _turn_through_the_write_path(
        chat_store, operator_id, [_assistant({"type": "text", "text": "one file"})]
    )
    async with migrated_sessions() as db:
        await db.execute(delete(SessionEvent).where(SessionEvent.session_id == session_id))
        await db.commit()
        report = await reprojection.check_session(db, session_id)

    outcome = one(report.turns).outcome
    assert isinstance(outcome, reprojection.Drifted)
    finding = one(outcome.findings)
    assert isinstance(finding, reprojection.RowCountMismatch)
    assert (finding.projected, finding.stored) == ((ConversationEventKind.MESSAGE_COMPLETED,), ())


async def test_a_turn_the_cursor_never_reached_is_skipped_rather_than_re_projected(
    chat_store, migrated_sessions, operator_id
) -> None:
    """The other era, #4178's: a session whose cursor a previous image never advanced.

    Its frames were projected by *something* — the rows are there — but the cursor stands where
    nothing has been projected, so re-projecting them would compare against a position no writer
    claimed.
    """
    session_id, _ = await _turn_through_the_write_path(
        chat_store, operator_id, [_assistant({"type": "text", "text": "one file"})]
    )
    async with migrated_sessions() as db:
        await db.execute(update(Session).where(Session.session_id == session_id).values(projected_frame_seq=0))
        await db.commit()
        report = await reprojection.check_session(db, session_id)

    turn = one(report.turns)
    assert turn.outcome == reprojection.Skipped(reason=reprojection.SkipReason.CURSOR_NEVER_REACHED)
    assert turn.checked_frames == 0


async def test_a_frame_recorded_past_the_cursor_is_counted_and_not_reported(
    chat_store, migrated_sessions, operator_id
) -> None:
    """A replica that died between recording a frame and projecting it is not drift.

    The cursor is what says so, and adoption is what will still redo the frame — so it is counted
    and reported as coverage rather than as a finding.
    """
    session_id, _ = await _turn_through_the_write_path(
        chat_store, operator_id, [_assistant({"type": "text", "text": "one file"})]
    )
    await chat_store.record_frame(
        session_id,
        FrameDirection.FROM_AGENT,
        "assistant",
        _assistant({"type": "text", "text": "and another"}, message_id="msg_2"),
    )

    async with migrated_sessions() as db:
        report = await reprojection.check_session(db, session_id)

    turn = one(report.turns)
    assert turn.outcome == reprojection.Agrees()
    assert (turn.checked_frames, turn.unprojected_frames) == (1, 1)


if __name__ == "__main__":
    pytest_bazel.main()
