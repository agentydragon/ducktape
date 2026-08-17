"""What a neutral event becomes as a row: which columns carry it, and what is left in the body."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest_bazel

from haku.console.chat_models import (
    AuthoredEventKind,
    ConversationEventKind,
    EventProvenance,
    LeaseExpiryReason,
    TurnOutcome,
)
from haku.console.database_schema import SessionEvent
from haku.console.x import session_events
from haku.console.x.conversation_events import (
    ActivityCompleted,
    ActivityStarted,
    Authored,
    ConversationEvent,
    FrameRange,
    MessageCompleted,
    MessageKey,
    OpaqueContent,
    Outcome,
    Reasoning,
    TextContent,
    TextDelta,
    ToolCallCompleted,
    ToolCallStarted,
    ToolReferences,
    TurnCompleted,
)

SESSION_ID = uuid4()
TURN_ID = uuid4()
NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
WHERE = FrameRange(11, 14)
MESSAGE = MessageKey(opened_at_frame_seq=11)


def stored(event: ConversationEvent) -> SessionEvent:
    row = session_events.row(event, session_id=SESSION_ID, turn_id=TURN_ID, now=NOW)
    assert row is not None
    return row


def test_a_frame_derived_event_carries_the_range_it_was_projected_from() -> None:
    row = stored(MessageCompleted(message=MESSAGE, text="done", agent_message_id="msg_1", provenance=WHERE))

    assert (row.kind, row.provenance) == (ConversationEventKind.MESSAGE_COMPLETED, EventProvenance.FRAME_RANGE)
    assert (row.source_first_frame_seq, row.source_last_frame_seq) == (11, 14)
    assert row.body == {"text": "done", "agent_message_id": "msg_1"}


def test_a_console_authored_event_takes_the_other_arm_and_no_range() -> None:
    """The distinction `session_messages` cannot make: no frames, rather than frames unrecorded."""
    row = stored(Reasoning(message=MESSAGE, summary=None, provenance=Authored()))

    assert row.provenance == EventProvenance.AUTHORED
    assert (row.source_first_frame_seq, row.source_last_frame_seq) == (None, None)


def test_a_tool_call_and_its_answer_are_two_rows_sharing_the_correlation_column() -> None:
    started = stored(
        ToolCallStarted(
            message=MESSAGE, call_id="toolu_1", tool_name="Bash", arguments={"command": "ls"}, provenance=WHERE
        )
    )
    completed = stored(
        ToolCallCompleted(
            call_id="toolu_1",
            content=TextContent(text="a.py\nb.py"),
            structured={"exit_code": 0},
            outcome=Outcome.SUCCEEDED,
            provenance=FrameRange(15, 15),
        )
    )

    assert started.call_id == completed.call_id == "toolu_1"
    assert started.body == {"tool_name": "Bash", "arguments": {"command": "ls"}}
    assert completed.body == {
        "content": {"shape": "text", "text": "a.py\nb.py"},
        "structured": {"exit_code": 0},
        "outcome": "succeeded",
    }


def test_a_result_that_named_tools_is_not_stored_as_prose() -> None:
    """The shape a renderer reading `content` as a string shows empty, kept as what it is."""
    references = stored(
        ToolCallCompleted(
            call_id="toolu_2",
            content=ToolReferences(tool_names=("Read", "Grep")),
            structured=None,
            outcome=Outcome.UNKNOWN,
            provenance=WHERE,
        )
    )
    opaque = stored(
        ToolCallCompleted(
            call_id="toolu_3",
            content=OpaqueContent(payload={"unknown": True}),
            structured=None,
            outcome=Outcome.UNKNOWN,
            provenance=WHERE,
        )
    )

    assert references.body["content"] == {"shape": "tool_references", "tool_names": ["Read", "Grep"]}
    assert opaque.body["content"] == {"shape": "opaque", "payload": {"unknown": True}}


def test_the_harness_narrating_a_step_is_a_pair_of_rows() -> None:
    started = stored(
        ActivityStarted(activity_id="task_1", call_id="toolu_1", description="searching", provenance=WHERE)
    )
    completed = stored(
        ActivityCompleted(activity_id="task_1", summary="found it", outcome=Outcome.SUCCEEDED, provenance=WHERE)
    )

    assert (started.kind, completed.kind) == (
        ConversationEventKind.ACTIVITY_STARTED,
        ConversationEventKind.ACTIVITY_COMPLETED,
    )
    # The call that opened the step rides in the body, because `ck_session_events_call_id` reserves
    # the correlation column for the two tool kinds and this is neither of them.
    assert started.body["call_id"] == "toolu_1"
    assert (started.call_id, completed.call_id) == (None, None)


def test_a_prompt_is_conversation_on_the_authored_arm() -> None:
    """The operator's half of the transcript, which no fold produces: a prompt is accepted before
    it crosses the wire, so it has frames nowhere and a turn not yet."""
    message_id = uuid4()
    asked = session_events.prompt_enqueued(session_id=SESSION_ID, message_id=message_id, text="list the files", now=NOW)

    assert (asked.kind, asked.provenance) == (AuthoredEventKind.PROMPT_ENQUEUED, EventProvenance.AUTHORED)
    assert (asked.turn_id, asked.source_first_frame_seq, asked.source_last_frame_seq, asked.call_id) == (None,) * 4
    assert asked.body == {"message_id": str(message_id), "text": "list the files"}


def test_a_fact_the_console_authored_names_no_turn_and_no_frames() -> None:
    """The second category: what happened *to* the session. It crossed no wire, so it is its own
    evidence — and it is the session's fact rather than an exchange's, which is what lets a session
    that never reached a turn have a stream at all."""
    taken = session_events.authored(
        session_events.SessionAdoptedBody(previous_holder="haku-console-a", holder="haku-console-b"),
        session_id=SESSION_ID,
        now=NOW,
    )

    assert (taken.kind, taken.provenance) == (AuthoredEventKind.SESSION_ADOPTED, EventProvenance.AUTHORED)
    assert (taken.turn_id, taken.source_first_frame_seq, taken.source_last_frame_seq, taken.call_id) == (None,) * 4
    assert taken.body == {"previous_holder": "haku-console-a", "holder": "haku-console-b"}


def test_the_kind_of_an_authored_row_follows_from_its_body() -> None:
    """One row per fact and no way to label it as another: the body is the discriminator."""
    lapsed = session_events.authored(
        session_events.LeaseExpiredBody(reason=LeaseExpiryReason.UNADOPTED, last_holder=None),
        session_id=SESSION_ID,
        now=NOW,
    )

    assert lapsed.kind == AuthoredEventKind.LEASE_EXPIRED
    assert lapsed.body == {"reason": "unadopted", "last_holder": None}


def test_the_two_events_with_a_durable_home_elsewhere_get_no_row() -> None:
    """A delta's prose is the message's own row; a turn's ending is the `session_turns` row."""
    unstored = [
        session_events.row(event, session_id=SESSION_ID, turn_id=TURN_ID, now=NOW)
        for event in (
            TextDelta(message=MESSAGE, text="par", provenance=WHERE),
            TurnCompleted(outcome=TurnOutcome.ANSWERED, usage=None, provenance=WHERE),
        )
    ]

    assert unstored == [None, None]


if __name__ == "__main__":
    pytest_bazel.main()
