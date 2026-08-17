"""What the conversation read model says about a session's bootstrap narration and its results."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest_bazel

from haku.console.chat_models import ConversationEventKind, EventProvenance, FrameDirection
from haku.console.database_schema import SessionEvent
from haku.console.x import session_views
from haku.console.x.session_store import BridgeAuthentication, SpaSession
from haku.console.x.setup_output import SETUP_OUTPUT_KIND, setup_output_frame


async def test_narration_reads_back_in_the_order_the_sandbox_produced_it(chat_store, operator_id) -> None:
    session, _ = await chat_store.create(operator_id, SpaSession())
    for line in ("Cloning into 'haku-state'...", "done.", "Starting Claude Code."):
        await chat_store.record_frame(
            session.session_id, FrameDirection.FROM_AGENT, SETUP_OUTPUT_KIND, setup_output_frame(line)
        )

    detail = await chat_store.get_operator_conversation(operator_id, session.session_id)

    assert [line.text for line in detail.narration] == [
        "Cloning into 'haku-state'...",
        "done.",
        "Starting Claude Code.",
    ]
    assert [line.frame_seq for line in detail.narration] == sorted(line.frame_seq for line in detail.narration)


async def test_two_identical_narration_lines_are_two_lines(chat_store, operator_id) -> None:
    """The rows carry no frame identity, so nothing may collapse a repeat into a replay: a
    bootstrap that says "retrying" twice retried twice."""
    session, _ = await chat_store.create(operator_id, SpaSession())
    for _ in range(2):
        await chat_store.record_frame(
            session.session_id, FrameDirection.FROM_AGENT, SETUP_OUTPUT_KIND, setup_output_frame("retrying")
        )

    detail = await chat_store.get_operator_conversation(operator_id, session.session_id)

    assert [line.text for line in detail.narration] == ["retrying", "retrying"]
    assert len({line.frame_seq for line in detail.narration}) == 2


async def test_narration_carries_only_this_session_and_only_setup_output(chat_store, operator_id) -> None:
    session, _ = await chat_store.create(operator_id, SpaSession())
    other, _ = await chat_store.create(operator_id, SpaSession())
    await chat_store.record_frame(
        session.session_id, FrameDirection.FROM_AGENT, SETUP_OUTPUT_KIND, setup_output_frame("mine")
    )
    await chat_store.record_frame(
        other.session_id, FrameDirection.FROM_AGENT, SETUP_OUTPUT_KIND, setup_output_frame("theirs")
    )
    await chat_store.record_frame(
        session.session_id, FrameDirection.FROM_AGENT, "result", {"type": "result", "uuid": "r1"}
    )

    detail = await chat_store.get_operator_conversation(operator_id, session.session_id)

    assert [line.text for line in detail.narration] == ["mine"]


async def test_a_session_that_narrated_nothing_reports_no_narration(chat_store, operator_id) -> None:
    session, _ = await chat_store.create(operator_id, SpaSession())

    detail = await chat_store.get_operator_conversation(operator_id, session.session_id)

    assert detail.narration == []


_STORED_CONTENT = {
    "toolu_text": {"shape": "text", "text": "a.py\nb.py"},
    "toolu_references": {"shape": "tool_references", "tool_names": ["Read", "Grep"]},
    "toolu_opaque": {"shape": "opaque", "payload": {"unknown": True}},
}


async def test_a_result_stored_in_any_shape_reads_back_as_text(chat_store, migrated_sessions, operator_id) -> None:
    """Only `text` is written now. The other two are rows this release inherited, and the SPA read
    parses every one of them — so dropping either arm would make an old transcript raise."""
    view, token = await chat_store.create(operator_id, SpaSession())
    assert await chat_store.authenticate_bridge(view.session_id, token) == BridgeAuthentication.ACCEPTED
    await chat_store.enqueue_prompt(operator_id, view.session_id, "list the files")
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

        calls = await session_views.tool_calls(db, view.session_id)

    assert {call_id: result.content for call_id, result in calls.results.items()} == {
        "toolu_text": "a.py\nb.py",
        "toolu_references": '["Read", "Grep"]',
        "toolu_opaque": '{"unknown": true}',
    }


if __name__ == "__main__":
    pytest_bazel.main()
