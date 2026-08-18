"""What the conversation read model says about a session's bootstrap narration and its results."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest_bazel

from haku.console.chat_models import SPA_ORIGIN, ConversationEventKind, EventProvenance, FrameDirection
from haku.console.database_schema import SessionEvent, SessionFrame
from haku.console.x import session_views
from haku.console.x.claude_code import projection
from haku.console.x.session_store import BridgeAuthentication
from haku.console.x.setup_output import SETUP_OUTPUT_KIND, setup_output_frame


async def _detail(chat_store, operator_id, session_id):
    """The conversation this session runs, read as the browser reads it."""
    return await chat_store.get_operator_conversation(operator_id, await chat_store.conversation_of(session_id))


async def test_narration_reads_back_in_the_order_the_sandbox_produced_it(chat_store, operator_id) -> None:
    session, _ = await chat_store.create(operator_id)
    for line in ("Cloning into 'haku-state'...", "done.", "Starting Claude Code."):
        await chat_store.record_frame(
            session.session_id, FrameDirection.FROM_AGENT, SETUP_OUTPUT_KIND, setup_output_frame(line)
        )

    detail = await _detail(chat_store, operator_id, session.session_id)

    assert [line.text for line in detail.session.narration] == [
        "Cloning into 'haku-state'...",
        "done.",
        "Starting Claude Code.",
    ]
    assert [line.frame_seq for line in detail.session.narration] == sorted(
        line.frame_seq for line in detail.session.narration
    )


async def test_two_identical_narration_lines_are_two_lines(chat_store, operator_id) -> None:
    """The rows carry no frame identity, so nothing may collapse a repeat into a replay: a
    bootstrap that says "retrying" twice retried twice."""
    session, _ = await chat_store.create(operator_id)
    for _ in range(2):
        await chat_store.record_frame(
            session.session_id, FrameDirection.FROM_AGENT, SETUP_OUTPUT_KIND, setup_output_frame("retrying")
        )

    detail = await _detail(chat_store, operator_id, session.session_id)

    assert [line.text for line in detail.session.narration] == ["retrying", "retrying"]
    assert len({line.frame_seq for line in detail.session.narration}) == 2


async def test_narration_carries_only_this_session_and_only_setup_output(chat_store, operator_id) -> None:
    session, _ = await chat_store.create(operator_id)
    other, _ = await chat_store.create(operator_id)
    await chat_store.record_frame(
        session.session_id, FrameDirection.FROM_AGENT, SETUP_OUTPUT_KIND, setup_output_frame("mine")
    )
    await chat_store.record_frame(
        other.session_id, FrameDirection.FROM_AGENT, SETUP_OUTPUT_KIND, setup_output_frame("theirs")
    )
    await chat_store.record_frame(
        session.session_id, FrameDirection.FROM_AGENT, "result", {"type": "result", "uuid": "r1"}
    )

    detail = await _detail(chat_store, operator_id, session.session_id)

    assert [line.text for line in detail.session.narration] == ["mine"]


async def test_a_session_that_narrated_nothing_reports_no_narration(chat_store, operator_id) -> None:
    session, _ = await chat_store.create(operator_id)

    detail = await _detail(chat_store, operator_id, session.session_id)

    assert detail.session.narration == []


_STORED_CONTENT = {"toolu_text": {"shape": "text", "text": "a.py\nb.py"}, "toolu_empty": {"shape": "text", "text": ""}}


async def test_a_stored_result_reads_back_as_its_text(chat_store, migrated_sessions, operator_id) -> None:
    """`text` is the only stored shape, and an empty result is a result rather than an absent one."""
    view, token = await chat_store.create(operator_id)
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, view.session_id, "list the files", SPA_ORIGIN)
    started = await chat_store.next_prompt(view.session_id)
    assert started is not None

    async with migrated_sessions() as db:
        for frame_seq, (call_id, content) in enumerate(_STORED_CONTENT.items(), start=7):
            db.add(
                SessionEvent(
                    session_id=view.session_id,
                    turn_id=started.turn_id,
                    kind=ConversationEventKind.TOOL_CALL_COMPLETED,
                    provenance=EventProvenance.FRAME_RANGE,
                    source_first_frame_seq=frame_seq,
                    source_last_frame_seq=frame_seq,
                    call_id=call_id,
                    body={"content": content, "structured": None, "outcome": "succeeded"},
                    created_at=datetime.now(UTC),
                )
            )
        await db.commit()

        calls = await session_views.tool_calls(db, view.session_id, since_frame_seq=None)

    assert {call_id: result.content for call_id, result in calls.results.items()} == {
        "toolu_text": "a.py\nb.py",
        "toolu_empty": "",
    }


def _frame(frame_seq: int, kind: str, payload: dict[str, Any]) -> SessionFrame:
    now = datetime.now(UTC)
    return SessionFrame(
        frame_seq=frame_seq,
        session_id=uuid4(),
        direction=FrameDirection.FROM_AGENT,
        kind=kind,
        payload=payload,
        created_at=now,
        updated_at=now,
    )


_INSPECTED = [
    _frame(1, SETUP_OUTPUT_KIND, setup_output_frame("cloning haku-state")),
    _frame(2, "system", {"type": "system", "subtype": "status"}),
    _frame(3, "system", {"type": "system", "subtype": "vcs_state_changed"}),
    _frame(4, "user", {"type": "user", "message": {"content": [{"type": "text", "text": "hi"}]}}),
    _frame(5, "result", {"type": "result", "subtype": "success"}),
]


def test_the_inspector_says_which_frames_the_fold_had_no_branch_for() -> None:
    """A frame class this release does not map is what a transcript is silently missing, and the key
    is the string to add a branch for."""
    page = session_views.frame_page(_INSPECTED, limit=len(_INSPECTED), conversation_id=uuid4())

    assert {frame.frame_seq: frame.unprojected for frame in page.frames} == {
        1: None,
        2: None,
        3: {"system/vcs_state_changed": 1},
        4: {"user/text": 1},
        5: None,
    }


def test_the_per_frame_counts_are_what_a_whole_session_fold_reports() -> None:
    """Per frame is exact rather than an approximation of the session-wide tally: a count keys off
    the frame's own class, never off what the fold accumulated before it. `setup_output` is the
    bridge's own envelope, which the fold refuses, so both sides exclude it."""
    page = session_views.frame_page(_INSPECTED, limit=len(_INSPECTED), conversation_id=uuid4())
    whole = projection.project_log(
        projection.RecordedFrame(frame_seq=row.frame_seq, payload=row.payload)
        for row in _INSPECTED
        if row.kind != SETUP_OUTPUT_KIND
    )

    tallied: dict[str, int] = {}
    for frame in page.frames:
        for kind, count in (frame.unprojected or {}).items():
            tallied[kind] = tallied.get(kind, 0) + count
    assert tallied == dict(whole.unprojected)


if __name__ == "__main__":
    pytest_bazel.main()
