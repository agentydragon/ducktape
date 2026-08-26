"""What the conversation read model says about a session's bootstrap narration and its results."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest_bazel

from haku.console.chat_models import (
    HARNESS_ORIGIN,
    SPA_ORIGIN,
    BridgeFrameKind,
    FrameDirection,
    ItemStatus,
    ItemType,
    MatrixOrigin,
    RuntimeKind,
)
from haku.console.database_schema import ConversationItem, SessionFrame
from haku.console.x import session_views
from haku.console.x.claude_code import projection
from haku.console.x.claude_code.testing.fold import whole_capture
from haku.console.x.claude_code.testing.wire import assistant, tool_result, tool_use_block
from haku.console.x.session_store import BridgeAuthentication
from haku.console.x.setup_output import SETUP_OUTPUT_KIND, setup_output_frame


async def _detail(chat_store, operator_id, session_id):
    """The conversation this session runs, read as the browser reads it."""
    return await chat_store.get_operator_conversation(operator_id, await chat_store.conversation_of(session_id))


async def test_narration_reads_back_in_the_order_the_sandbox_produced_it(chat_store, operator_id) -> None:
    session, _ = await chat_store.create(operator_id)
    for line in ("Cloning into 'haku-state'...", "done.", "Starting Claude Code."):
        await chat_store.narrate(session.session_id, line)

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
        await chat_store.narrate(session.session_id, "retrying")

    detail = await _detail(chat_store, operator_id, session.session_id)

    assert [line.text for line in detail.session.narration] == ["retrying", "retrying"]
    assert len({line.frame_seq for line in detail.session.narration}) == 2


async def test_narration_carries_only_this_session_and_only_setup_output(chat_store, operator_id) -> None:
    session, _ = await chat_store.create(operator_id)
    other, _ = await chat_store.create(operator_id)
    await chat_store.narrate(session.session_id, "mine")
    await chat_store.narrate(other.session_id, "theirs")
    await chat_store.record_frame(
        session.session_id, FrameDirection.FROM_AGENT, BridgeFrameKind.HARNESS_FRAME, {"type": "result", "uuid": "r1"}
    )

    detail = await _detail(chat_store, operator_id, session.session_id)

    assert [line.text for line in detail.session.narration] == ["mine"]


async def test_a_session_that_narrated_nothing_reports_no_narration(chat_store, operator_id) -> None:
    session, _ = await chat_store.create(operator_id)

    detail = await _detail(chat_store, operator_id, session.session_id)

    assert detail.session.narration == []


async def test_a_calls_output_reads_back_as_the_items_text(chat_store, operator_id) -> None:
    """A call's showable output is its segments like any other item's prose, and a call that printed
    nothing is an empty item rather than an absent one — which is what a reader needs to tell "it
    said nothing" from "it has not answered yet"."""
    view, token = await chat_store.create(operator_id)
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, view.session_id, "list the files", SPA_ORIGIN)
    started = await chat_store.next_prompt(view.session_id)
    assert started is not None

    for frame_seq, (call_id, output) in enumerate([("toolu_text", "a.py\nb.py"), ("toolu_empty", "")], start=7):
        await chat_store.apply_frame(
            view.session_id,
            started.turn_id,
            frame_seq,
            whole_capture(
                [
                    projection.RecordedFrame(
                        frame_seq=frame_seq, payload=assistant(tool_use_block(call_id, "Bash", {}))
                    ),
                    projection.RecordedFrame(frame_seq=frame_seq, payload=tool_result(call_id, output)),
                ]
            ).events,
        )

    detail = await _detail(chat_store, operator_id, view.session_id)
    calls = [item for item in detail.session.items if item.item_type is ItemType.TOOL_CALL]

    assert {item.call_id: item.text for item in calls} == {"toolu_text": "a.py\nb.py", "toolu_empty": ""}


def _frame(frame_seq: int, kind: BridgeFrameKind, payload: dict[str, Any]) -> SessionFrame:
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
    _frame(2, BridgeFrameKind.HARNESS_FRAME, {"type": "system", "subtype": "status"}),
    _frame(3, BridgeFrameKind.HARNESS_FRAME, {"type": "system", "subtype": "vcs_state_changed"}),
    _frame(
        4, BridgeFrameKind.HARNESS_FRAME, {"type": "user", "message": {"content": [{"type": "text", "text": "hi"}]}}
    ),
    _frame(5, BridgeFrameKind.HARNESS_FRAME, {"type": "result", "subtype": "success"}),
]


def test_the_inspector_keeps_native_payloads_opaque() -> None:
    page = session_views.frame_page(
        _INSPECTED, limit=len(_INSPECTED), conversation_id=uuid4(), runtime_kind=RuntimeKind.CLAUDE_CODE
    )

    assert [(frame.kind, frame.payload) for frame in page.frames] == [(row.kind, row.payload) for row in _INSPECTED]
    assert all("native_kind" not in frame.model_fields_set for frame in page.frames)
    assert all("unprojected" not in frame.model_fields_set for frame in page.frames)


def _prompt_row(origin: dict[str, object] | None) -> ConversationItem:
    now = datetime.now(UTC)
    return ConversationItem(
        item_id=uuid4(),
        conversation_id=uuid4(),
        item_type=ItemType.PROMPT,
        status=ItemStatus.COMPLETE,
        opened_seq=1,
        closed_seq=3,
        item_text="a prompt",
        origin=origin,
        created_at=now,
        updated_at=now,
    )


def test_a_prompts_origin_reads_back_typed_for_every_arm() -> None:
    """The view says whose voice a prompt is — including the harness's own, which the renderer
    must be able to tell from the operator's before anything writes it (readers ship a release
    ahead of the writer; see AGENTS.md § Vocabularies across a roll)."""
    assert session_views.item_view(_prompt_row({"kind": "spa"})).origin == SPA_ORIGIN
    assert session_views.item_view(_prompt_row({"kind": "harness"})).origin == HARNESS_ORIGIN
    matrix = session_views.item_view(_prompt_row({"kind": "matrix", "address": "!r:x", "refs": ["$e"]})).origin
    assert isinstance(matrix, MatrixOrigin)
    assert matrix.address == "!r:x"


def test_an_item_without_an_origin_reports_none() -> None:
    assert session_views.item_view(_prompt_row(None)).origin is None


if __name__ == "__main__":
    pytest_bazel.main()
