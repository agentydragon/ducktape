"""What the conversation read model says about a session's bootstrap narration and its results."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest_bazel

from haku.console.conversation.conversation_event import FrameRange
from haku.console.conversation.item_reads import ToolCallItem
from haku.console.conversation.item_vocabulary import ToolOutcome
from haku.console.conversation.prompt_origin import SPA_ORIGIN
from haku.console.database_schema import SessionFrame
from haku.console.harnesses.kind import HarnessKind
from haku.console.session import conversation_views
from haku.console.session.session_frames import FrameDirection, SessionFrameKind
from haku.console.session.setup_output import SETUP_OUTPUT_KIND, setup_output_frame
from haku.console.session.store import RunnerConnectionAuthentication
from haku.console.x.conversation_events import CallRef, ItemSegment, ToolCallCompleted, ToolCallStarted


async def _detail(session_store, operator_id, session_id):
    """The conversation this session runs, read as the browser reads it."""
    return await session_store.get_operator_conversation(operator_id, await session_store.conversation_of(session_id))


async def test_narration_reads_back_in_the_order_the_sandbox_produced_it(session_store, operator_id) -> None:
    session, _ = await session_store.create(operator_id, harness_kind=HarnessKind.CLAUDE_CODE)
    for line in ("Cloning into 'haku-state'...", "done.", "Starting Claude Code."):
        await session_store.narrate(session.session_id, line)

    detail = await _detail(session_store, operator_id, session.session_id)

    assert [line.text for line in detail.narration] == [
        "Cloning into 'haku-state'...",
        "done.",
        "Starting Claude Code.",
    ]
    assert [line.frame_seq for line in detail.narration] == sorted(line.frame_seq for line in detail.narration)


async def test_two_identical_narration_lines_are_two_lines(session_store, operator_id) -> None:
    """The rows carry no frame identity, so nothing may collapse a repeat into a replay: a
    bootstrap that says "retrying" twice retried twice."""
    session, _ = await session_store.create(operator_id, harness_kind=HarnessKind.CLAUDE_CODE)
    for _ in range(2):
        await session_store.narrate(session.session_id, "retrying")

    detail = await _detail(session_store, operator_id, session.session_id)

    assert [line.text for line in detail.narration] == ["retrying", "retrying"]
    assert len({line.frame_seq for line in detail.narration}) == 2


async def test_narration_carries_only_this_session_and_only_setup_output(session_store, operator_id) -> None:
    session, _ = await session_store.create(operator_id, harness_kind=HarnessKind.CLAUDE_CODE)
    other, _ = await session_store.create(operator_id, harness_kind=HarnessKind.CLAUDE_CODE)
    await session_store.narrate(session.session_id, "mine")
    await session_store.narrate(other.session_id, "theirs")
    await session_store.record_frame(
        session.session_id, FrameDirection.FROM_AGENT, SessionFrameKind.HARNESS_FRAME, {"type": "result", "uuid": "r1"}
    )

    detail = await _detail(session_store, operator_id, session.session_id)

    assert [line.text for line in detail.narration] == ["mine"]


async def test_a_session_that_narrated_nothing_reports_no_narration(session_store, operator_id) -> None:
    session, _ = await session_store.create(operator_id, harness_kind=HarnessKind.CLAUDE_CODE)

    detail = await _detail(session_store, operator_id, session.session_id)

    assert detail.narration == []


async def test_a_calls_output_reads_back_as_the_items_text(session_store, operator_id) -> None:
    """A call's showable output is its segments like any other item's prose, and a call that printed
    nothing is an empty item rather than an absent one — which is what a reader needs to tell "it
    said nothing" from "it has not answered yet"."""
    view, token = await session_store.create(operator_id, harness_kind=HarnessKind.CLAUDE_CODE)
    assert (
        await session_store.authenticate_runner_connection(view.session_id, token)
        == RunnerConnectionAuthentication.ACCEPTED
    )
    await session_store.enqueue_prompt(operator_id, view.session_id, "list the files", SPA_ORIGIN)
    started = await session_store.next_prompt(view.session_id)
    assert started is not None

    for frame_seq, (call_id, output) in enumerate([("toolu_text", "a.py\nb.py"), ("toolu_empty", "")], start=7):
        where = FrameRange(frame_seq, frame_seq)
        segments = [ItemSegment(item=CallRef(call_id=call_id), text=output, provenance=where)] if output else []
        await session_store.apply_frame(
            view.session_id,
            started.turn_id,
            frame_seq,
            [
                ToolCallStarted(call_id=call_id, tool_name="Bash", arguments={}, provenance=where),
                *segments,
                ToolCallCompleted(
                    item=CallRef(call_id=call_id), structured=None, outcome=ToolOutcome.SUCCEEDED, provenance=where
                ),
            ],
        )

    detail = await _detail(session_store, operator_id, view.session_id)
    calls = [item for item in detail.items if isinstance(item, ToolCallItem)]

    assert {item.call_id: item.content for item in calls} == {"toolu_text": "a.py\nb.py", "toolu_empty": ""}


def _frame(frame_seq: int, kind: SessionFrameKind, payload: dict[str, Any]) -> SessionFrame:
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
    _frame(2, SessionFrameKind.HARNESS_FRAME, {"type": "system", "subtype": "status"}),
    _frame(3, SessionFrameKind.HARNESS_FRAME, {"type": "system", "subtype": "vcs_state_changed"}),
    _frame(
        4, SessionFrameKind.HARNESS_FRAME, {"type": "user", "message": {"content": [{"type": "text", "text": "hi"}]}}
    ),
    _frame(5, SessionFrameKind.HARNESS_FRAME, {"type": "result", "subtype": "success"}),
]


def test_the_inspector_keeps_native_payloads_opaque() -> None:
    page = conversation_views.frame_page(
        _INSPECTED, limit=len(_INSPECTED), conversation_id=uuid4(), harness_kind=HarnessKind.CLAUDE_CODE
    )

    assert [(frame.kind, frame.payload) for frame in page.frames] == [(row.kind, row.payload) for row in _INSPECTED]
    assert all("native_kind" not in frame.model_fields_set for frame in page.frames)
    assert all("unprojected" not in frame.model_fields_set for frame in page.frames)


if __name__ == "__main__":
    pytest_bazel.main()
