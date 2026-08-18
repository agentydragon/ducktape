"""What a neutral event becomes as a row: which columns carry it, and what is left in the body."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_bazel
from pydantic import ValidationError

from haku.console.chat_models import (
    SPA_ORIGIN,
    AuthoredEventKind,
    ConversationEventKind,
    EventProvenance,
    LeaseExpiryReason,
    MatrixOrigin,
    StoredEventKind,
    TurnOutcome,
)
from haku.console.database_schema import SessionEvent
from haku.console.x import session_events
from haku.console.x.conversation_events import (
    Authored,
    ConversationEvent,
    FrameRange,
    MessageCompleted,
    MessageKey,
    Outcome,
    Reasoning,
    TextDelta,
    ToolCallCompleted,
    ToolCallStarted,
    TurnCompleted,
)
from util.sqlalchemy_types import UnknownValue

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


def test_a_frame_derived_kind_that_names_no_frames_is_refused_rather_than_downgraded() -> None:
    """Every kind this writes is one a fold produced, so the other arm is an adapter bug.

    Writing it as `authored` instead would land the failure on the read, where one such row makes a
    whole session's transcript unreadable.
    """
    with pytest.raises(ValueError, match="projected from frames"):
        session_events.row(
            Reasoning(message=MESSAGE, summary=None, provenance=Authored()),
            session_id=SESSION_ID,
            turn_id=TURN_ID,
            now=NOW,
        )


def test_a_tool_call_and_its_answer_are_two_rows_sharing_the_correlation_column() -> None:
    started = stored(
        ToolCallStarted(
            message=MESSAGE, call_id="toolu_1", tool_name="Bash", arguments={"command": "ls"}, provenance=WHERE
        )
    )
    completed = stored(
        ToolCallCompleted(
            call_id="toolu_1",
            content="a.py\nb.py",
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


def test_every_result_is_now_stored_as_text() -> None:
    """The variant has one arm, and every row is written into it."""
    rendered = stored(
        ToolCallCompleted(
            call_id="toolu_2",
            content='[{"tool_name": "Read", "type": "tool_reference"}]',
            structured=None,
            outcome=Outcome.UNKNOWN,
            provenance=WHERE,
        )
    )

    assert rendered.body["content"] == {"shape": "text", "text": '[{"tool_name": "Read", "type": "tool_reference"}]'}


def test_a_prompt_is_conversation_on_the_authored_arm() -> None:
    """The operator's half of the transcript, which no fold produces: a prompt is accepted before
    it crosses the wire, so it has frames nowhere and a turn not yet."""
    message_id = uuid4()
    asked = session_events.prompt_enqueued(
        session_id=SESSION_ID, message_id=message_id, text="list the files", origin=SPA_ORIGIN, now=NOW
    )

    assert (asked.kind, asked.provenance) == (AuthoredEventKind.PROMPT_ENQUEUED, EventProvenance.AUTHORED)
    assert (asked.turn_id, asked.source_first_frame_seq, asked.source_last_frame_seq, asked.call_id) == (None,) * 4
    assert asked.body == {"message_id": str(message_id), "text": "list the files", "origin": {"kind": "spa"}}


def test_a_room_prompt_names_the_room_as_well_as_the_events() -> None:
    """A bare event id cannot tell a sibling room's copy of a prompt from this room's, which is
    exactly the question the surface reading this asks. Both strings stay the channel's own."""
    asked = session_events.prompt_enqueued(
        session_id=SESSION_ID,
        message_id=uuid4(),
        text="hi",
        origin=MatrixOrigin(address="!room:example.org", refs=("$a", "$b")),
        now=NOW,
    )

    assert asked.body["origin"] == {"kind": "matrix", "address": "!room:example.org", "refs": ["$a", "$b"]}


def test_a_prompt_body_without_an_origin_is_rejected() -> None:
    """The reason the SPA is a named arm rather than the absent one: there is no default to fall
    back to, because reading a missing key as "typed into a browser" would tell every attached room
    it owes a copy of a prompt the room may already be showing. `ck_session_events_prompt_origin`
    keeps such a row out of the table, so this shape is a bug, not an era."""
    stored = {"message_id": str(uuid4()), "text": "no surface named"}

    with pytest.raises(ValidationError):
        session_events.PromptBody.model_validate(stored)


def test_a_fact_the_console_authored_names_no_turn_and_no_frames() -> None:
    """The second category: what happened *to* the session. It crossed no wire, and it is the
    session's fact rather than an exchange's — which is what lets a session that never reached a
    turn have a stream at all."""
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


# One stored body per kind the column may hold. A kind added without an entry fails
# `test_every_kind_the_column_may_hold_can_be_read_back` rather than reaching a replica that
# cannot parse it.
_BODIES: dict[StoredEventKind, dict[str, object]] = {
    ConversationEventKind.MESSAGE_COMPLETED: {"text": "done", "agent_message_id": "msg_1"},
    ConversationEventKind.REASONING: {"summary": "thinking about it"},
    ConversationEventKind.TOOL_CALL_STARTED: {"tool_name": "Bash", "arguments": {"command": "ls"}},
    ConversationEventKind.TOOL_CALL_COMPLETED: {
        "content": {"shape": "text", "text": "a.py"},
        "structured": {"exit_code": 0},
        "outcome": "succeeded",
    },
    AuthoredEventKind.PROMPT_ENQUEUED: {"message_id": str(uuid4()), "text": "hello", "origin": {"kind": "spa"}},
    AuthoredEventKind.PROMPT_REJECTED: {"reason": "turn_in_flight", "text": "wait"},
    AuthoredEventKind.UNREADABLE_INPUT: {"media_type": "m.image"},
    AuthoredEventKind.SESSION_ADOPTED: {"previous_holder": None, "holder": "haku-console-a"},
    AuthoredEventKind.LEASE_EXPIRED: {"reason": "unadopted", "last_holder": None},
    AuthoredEventKind.TURN_ABORTED: {},
    AuthoredEventKind.TURN_STARTED: {},
    AuthoredEventKind.TURN_ENDED: {"outcome": "answered"},
    AuthoredEventKind.SESSION_PROVISIONING: {},
    AuthoredEventKind.SESSION_ENDED: {"status": "failed", "error": "the claim was never satisfied"},
    AuthoredEventKind.SETUP_NARRATION: {"text": "cloning haku-state"},
}


def test_every_kind_the_column_may_hold_can_be_read_back() -> None:
    """The property a roll depends on, which is stronger than the type check behind it.

    Tolerance covers the kind a *later* release adds; it cannot cover one this release already
    holds and has no body for, because `body_of` would then be reading a kind it does name. So this
    says the vocabulary is complete — every kind the column may hold parses — which is what lets a
    writer for these ship without a second release.
    """
    unreadable = [
        kind
        for kind in (*ConversationEventKind, *AuthoredEventKind)
        if session_events.body_of(
            SessionEvent(kind=kind, body=_BODIES[kind], created_at=NOW, provenance=EventProvenance.AUTHORED)
        )
        is None
    ]

    assert unreadable == []


def test_a_row_of_a_kind_this_release_has_no_words_for_reads_as_one() -> None:
    """What the previous image sees for the length of a roll once a release adds a kind.

    The point is that it is a value: the read that produced it carries every other row of that
    conversation, and a subscriber can position and skip this one instead of losing all of them.
    """
    row = SessionEvent(
        session_id=SESSION_ID,
        turn_id=None,
        kind=UnknownValue("provisioning_started"),
        provenance=EventProvenance.AUTHORED,
        source_first_frame_seq=None,
        source_last_frame_seq=None,
        call_id=None,
        body={"reason": "a field this release has never heard of"},
        created_at=NOW,
    )

    assert session_events.body_of(row) == session_events.UnknownEventBody(
        kind="provisioning_started", body={"reason": "a field this release has never heard of"}
    )


def test_a_body_carrying_a_field_this_release_does_not_know_still_reads() -> None:
    """The other half of the same roll, and the one that bites without a new kind at all: the
    release that adds a field to a body writes rows the previous image is still reading, so a body
    that forbade extras would raise on every one of them."""
    row = SessionEvent(
        session_id=SESSION_ID,
        turn_id=None,
        kind=AuthoredEventKind.LEASE_EXPIRED,
        provenance=EventProvenance.AUTHORED,
        source_first_frame_seq=None,
        source_last_frame_seq=None,
        call_id=None,
        body={"reason": "unadopted", "last_holder": None, "swept_by": "a field added later"},
        created_at=NOW,
    )

    assert session_events.body_of(row) == session_events.LeaseExpiredBody(
        reason=LeaseExpiryReason.UNADOPTED, last_holder=None
    )


def test_the_two_events_with_a_durable_home_elsewhere_get_no_row() -> None:
    """A delta's prose is the message's own row; a turn's ending is the `session_turns` row."""
    unstored = [
        session_events.row(event, session_id=SESSION_ID, turn_id=TURN_ID, now=NOW)
        for event in (
            TextDelta(message=MESSAGE, text="par", provenance=WHERE),
            TurnCompleted(outcome=TurnOutcome.ANSWERED, provenance=WHERE),
        )
    ]

    assert unstored == [None, None]


if __name__ == "__main__":
    pytest_bazel.main()
