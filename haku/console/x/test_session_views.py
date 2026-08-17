"""What the conversation read model says about a session's bootstrap narration and its results."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_bazel

from haku.console.chat_models import ConversationEventKind, EventProvenance, FrameDirection
from haku.console.database_schema import SessionEvent
from haku.console.x import session_views
from haku.console.x.session_store import SpaSession
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


@pytest.mark.parametrize(
    ("stored_content", "rendered"),
    [
        ({"shape": "text", "text": "a.py\nb.py"}, "a.py\nb.py"),
        ({"shape": "tool_references", "tool_names": ["Read", "Grep"]}, '["Read", "Grep"]'),
        ({"shape": "opaque", "payload": {"unknown": True}}, '{"unknown": true}'),
    ],
)
async def test_a_result_stored_in_any_shape_reads_back_as_text(
    chat_store, migrated_sessions, operator_id, stored_content, rendered
) -> None:
    """Only the first shape is written now. The other two are rows this release inherited, and the
    SPA read parses each one — so dropping either arm would make an old transcript raise."""
    session, _ = await chat_store.create(operator_id, SpaSession())
    async with migrated_sessions() as db:
        db.add(
            SessionEvent(
                session_id=session.session_id,
                turn_id=None,
                kind=ConversationEventKind.TOOL_CALL_COMPLETED,
                provenance=EventProvenance.FRAME_RANGE,
                source_first_frame_seq=1,
                source_last_frame_seq=1,
                call_id="toolu_1",
                body={"content": stored_content, "structured": None, "outcome": "succeeded"},
                created_at=datetime.now(UTC),
            )
        )
        await db.commit()

        calls = await session_views.tool_calls(db, session.session_id)

    assert calls.results["toolu_1"].content == rendered


if __name__ == "__main__":
    pytest_bazel.main()
