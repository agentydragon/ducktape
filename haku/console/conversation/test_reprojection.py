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

from haku.console.chat_models import SPA_ORIGIN, BridgeFrameKind, ConversationEventKind, FrameDirection, ItemType
from haku.console.conversation import reprojection
from haku.console.database_schema import ConversationEventRow, ConversationItem, Session
from haku.console.session.store import BridgeAuthentication
from haku.console.x.claude_code.runtime import ClaudeRuntimeAdapter
from haku.console.x.claude_code.testing.wire import content_block_stop, input_json_delta, tool_use_start
from haku.console.x.runtime import Checkpoint
from haku.console.x.runtime_catalog import projection_registry
from haku.console.x.session_events import TurnAbortedBody
from haku.runtime.x.bridge.protocol import HarnessFrame

RUNTIMES = projection_registry()


def _assistant(*blocks: dict[str, Any], message_id: str = "msg_1") -> dict[str, Any]:
    """One native `assistant` frame; the id remains part of the forensic payload."""
    return {"type": "assistant", "message": {"id": message_id, "role": "assistant", "content": list(blocks)}}


def _tool_result(call_id: str, text: str) -> dict[str, Any]:
    return {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call_id, "content": text}]},
        "tool_use_result": {"exit_code": 0},
    }


async def _turn_through_the_write_path(session_store, operator_id, frames: list[dict[str, Any]]) -> tuple[UUID, UUID]:
    """One session and one open turn, with *frames* recorded and projected as the turn loop does."""
    view, token = await session_store.create(operator_id)
    session_id = view.session_id
    assert await session_store.authenticate_bridge(session_id, token) == BridgeAuthentication.ACCEPTED
    await session_store.enqueue_prompt(operator_id, session_id, "what is in here?", SPA_ORIGIN)
    started = await session_store.next_prompt(session_id)
    assert started is not None
    handler = ClaudeRuntimeAdapter().turn_handler()
    for payload in frames:
        frame = payload
        recorded = await session_store.record_frame(
            session_id, FrameDirection.FROM_AGENT, BridgeFrameKind.HARNESS_FRAME, frame
        )
        effects = handler.apply(frame_seq=recorded.frame_seq, frame=HarnessFrame(frame=payload))
        if effects.checkpoint is Checkpoint.ADVANCE:
            await session_store.apply_frame(session_id, started.turn_id, recorded.frame_seq, effects.events)
        else:
            assert not effects.events
    return session_id, started.turn_id


async def test_a_session_the_write_path_projected_agrees_with_itself(
    session_store, migrated_sessions, operator_id
) -> None:
    session_id, turn_id = await _turn_through_the_write_path(
        session_store,
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
        report = await reprojection.check_session(db, session_id, runtimes=RUNTIMES)

    turn = one(report.turns)
    assert (turn.turn_id, turn.outcome) == (turn_id, reprojection.Agrees())
    # Three rows for the reasoning item, the call's ask, the two its answer wrote, and the message
    # the turn ended still writing into — opened and said, with its close belonging to `end_turn`.
    assert turn.stored_rows == 8
    assert turn.unprojected_frames == 0
    # And the invariant the whole shape exists for: every item's text is exactly its segments.
    assert report.items == ()


async def test_a_streamed_tool_declaration_agrees_with_its_multiframe_provenance(
    session_store, migrated_sessions, operator_id
) -> None:
    session_id, _ = await _turn_through_the_write_path(
        session_store,
        operator_id,
        [
            tool_use_start("toolu_1", "Bash", index=1),
            input_json_delta('{"command": "ls"}', index=1),
            content_block_stop(index=1),
            _tool_result("toolu_1", "a.py"),
        ],
    )

    async with migrated_sessions() as db:
        report = await reprojection.check_session(db, session_id, runtimes=RUNTIMES)

    assert one(report.turns).outcome == reprojection.Agrees()


async def test_an_aborted_turn_still_agrees_with_its_frames(session_store, migrated_sessions, operator_id) -> None:
    """The authored arm is out of scope, and `turn_started`/`turn_ended` are the members of it that
    name a turn, so the per-turn read excludes it: no frame projects to a row nothing sent, and
    comparing it against a re-fold would report drift on every turn.
    """
    session_id, turn_id = await _turn_through_the_write_path(
        session_store, operator_id, [_assistant({"type": "text", "text": "one file"})]
    )
    await session_store.end_turn(turn_id, TurnAbortedBody())

    async with migrated_sessions() as db:
        report = await reprojection.check_session(db, session_id, runtimes=RUNTIMES)

    assert one(report.turns).outcome == reprojection.Agrees()


async def test_a_row_whose_body_was_edited_is_reported_against_its_frame(
    session_store, migrated_sessions, operator_id
) -> None:
    """The comparison's whole purpose: a stored row the frames do not project to any more."""
    session_id, _ = await _turn_through_the_write_path(
        session_store,
        operator_id,
        [_assistant({"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}})],
    )
    async with migrated_sessions() as db:
        await db.execute(
            update(ConversationEventRow)
            .where(
                ConversationEventRow.session_id == session_id,
                ConversationEventRow.kind == ConversationEventKind.ITEM_STARTED,
                ConversationEventRow.body["item_type"].astext == "tool_call",
            )
            .values(body={"item_type": "tool_call", "call_id": "toolu_1", "tool_name": "Write", "arguments": {}})
        )
        await db.commit()
        report = await reprojection.check_session(db, session_id, runtimes=RUNTIMES)

    outcome = one(report.turns).outcome
    assert isinstance(outcome, reprojection.Drifted)
    mismatch = one(outcome.findings)
    assert isinstance(mismatch, reprojection.RowMismatch)
    assert [difference.field for difference in mismatch.differences] == ["body"]
    assert "Bash" in mismatch.differences[0].projected
    assert "Write" in mismatch.differences[0].stored


async def test_a_row_that_is_gone_is_a_count_mismatch_rather_than_a_silent_pass(
    session_store, migrated_sessions, operator_id
) -> None:
    session_id, _ = await _turn_through_the_write_path(
        session_store,
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
            delete(ConversationEventRow).where(
                ConversationEventRow.session_id == session_id,
                ConversationEventRow.kind == ConversationEventKind.ITEM_STARTED,
                ConversationEventRow.body["item_type"].astext == "tool_call",
            )
        )
        await db.commit()
        report = await reprojection.check_session(db, session_id, runtimes=RUNTIMES)

    outcome = one(report.turns).outcome
    assert isinstance(outcome, reprojection.Drifted)
    finding = one(outcome.findings)
    assert isinstance(finding, reprojection.RowCountMismatch)
    assert finding.projected == (
        ConversationEventKind.ITEM_STARTED,
        ConversationEventKind.ITEM_STARTED,
        ConversationEventKind.ITEM_SEGMENT,
    )
    assert finding.stored == (ConversationEventKind.ITEM_STARTED, ConversationEventKind.ITEM_SEGMENT)


async def test_a_turn_with_frames_and_no_rows_is_drift(session_store, migrated_sessions, operator_id) -> None:
    """No writer leaves a projected frame without its rows, so a turn with none is reported against
    the frame that should have written them, exactly as a single missing row is."""
    session_id, _ = await _turn_through_the_write_path(
        session_store, operator_id, [_assistant({"type": "text", "text": "one file"})]
    )
    async with migrated_sessions() as db:
        await db.execute(delete(ConversationEventRow).where(ConversationEventRow.session_id == session_id))
        await db.commit()
        report = await reprojection.check_session(db, session_id, runtimes=RUNTIMES)

    outcome = one(report.turns).outcome
    assert isinstance(outcome, reprojection.Drifted)
    finding = one(outcome.findings)
    assert isinstance(finding, reprojection.RowCountMismatch)
    assert (finding.projected, finding.stored) == (
        (ConversationEventKind.ITEM_STARTED, ConversationEventKind.ITEM_SEGMENT),
        (),
    )


async def test_a_turn_the_cursor_never_reached_is_skipped_rather_than_re_projected(
    session_store, migrated_sessions, operator_id
) -> None:
    """A session whose cursor was never advanced: its frames were projected by *something*, since
    the rows are there, but re-projecting them would compare against a position no writer claimed.
    """
    session_id, _ = await _turn_through_the_write_path(
        session_store, operator_id, [_assistant({"type": "text", "text": "one file"})]
    )
    async with migrated_sessions() as db:
        await db.execute(update(Session).where(Session.session_id == session_id).values(projected_frame_seq=0))
        await db.commit()
        report = await reprojection.check_session(db, session_id, runtimes=RUNTIMES)

    turn = one(report.turns)
    assert turn.outcome == reprojection.Skipped(reason=reprojection.SkipReason.CURSOR_NEVER_REACHED)
    assert turn.checked_frames == 0


async def test_a_frame_recorded_past_the_cursor_is_counted_and_not_reported(
    session_store, migrated_sessions, operator_id
) -> None:
    """A replica that died between recording a frame and projecting it is not drift: the cursor says
    so and adoption will still redo the frame, so it is coverage rather than a finding.
    """
    session_id, _ = await _turn_through_the_write_path(
        session_store, operator_id, [_assistant({"type": "text", "text": "one file"})]
    )
    await session_store.record_frame(
        session_id,
        FrameDirection.FROM_AGENT,
        BridgeFrameKind.HARNESS_FRAME,
        _assistant({"type": "text", "text": "and another"}, message_id="msg_2"),
    )

    async with migrated_sessions() as db:
        report = await reprojection.check_session(db, session_id, runtimes=RUNTIMES)

    turn = one(report.turns)
    assert turn.outcome == reprojection.Agrees()
    assert (turn.checked_frames, turn.unprojected_frames) == (1, 1)


async def test_an_items_text_edited_out_from_under_its_segments_is_a_finding(
    session_store, migrated_sessions, operator_id
) -> None:
    """The check the old shape could not be given. `conversation_item.text` is a fold of the item's
    segments and never a second authority for them, so a row that stopped agreeing with the log that
    produced it is drift rather than a disagreement nobody can adjudicate."""
    session_id, _ = await _turn_through_the_write_path(
        session_store, operator_id, [_assistant({"type": "text", "text": "one file"})]
    )
    async with migrated_sessions() as db:
        await db.execute(
            update(ConversationItem)
            .where(ConversationItem.session_id == session_id, ConversationItem.item_type == ItemType.MESSAGE)
            .values(item_text="two files")
        )
        await db.commit()
        report = await reprojection.check_session(db, session_id, runtimes=RUNTIMES)

    drifted = one(report.items)
    assert (drifted.folded, drifted.stored) == ("one file", "two files")


if __name__ == "__main__":
    pytest_bazel.main()
