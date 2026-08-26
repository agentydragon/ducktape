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
    ItemType,
    LeaseExpiryReason,
    MatrixOrigin,
    ReasoningDisclosure,
    StoredEventKind,
    ToolOutcome,
)
from haku.console.database_schema import ConversationEvent as ConversationEventRow
from haku.console.x import session_events
from haku.console.x.conversation_events import (
    CallRef,
    ConversationEvent,
    FrameRange,
    ItemSegment,
    MessageCompleted,
    OpenRef,
    ReasoningStarted,
    ToolCallCompleted,
    ToolCallStarted,
    TurnAnswered,
    TurnCompleted,
)
from util.sqlalchemy_types import UnknownValue

CONVERSATION_ID = uuid4()
SESSION_ID = uuid4()
TURN_ID = uuid4()
ITEM_ID = uuid4()
NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
WHERE = FrameRange(11, 14)


def stored(event: ConversationEvent) -> ConversationEventRow:
    """One event through both halves of the write path: what kind it is, and the row that carries it.

    Split in production because the two answer to different owners — the fold says what an event
    means and the log writer says where it goes — so a test of the mapping has to put them back
    together.
    """
    said = session_events.stored(event)
    assert said is not None
    kind, body = said
    provenance = event.provenance
    assert isinstance(provenance, FrameRange)
    return session_events.item_row(
        kind,
        body,
        conversation_id=CONVERSATION_ID,
        event_seq=7,
        item_id=ITEM_ID,
        session_id=SESSION_ID,
        turn_id=TURN_ID,
        provenance=provenance,
        now=NOW,
    )


def test_a_frame_derived_event_carries_the_range_it_was_projected_from() -> None:
    row = stored(MessageCompleted(backend_item_id="msg_1", provenance=WHERE))

    assert (row.kind, row.provenance) == (ConversationEventKind.ITEM_COMPLETED, EventProvenance.FRAME_RANGE)
    assert (row.source_first_frame_seq, row.source_last_frame_seq) == (11, 14)
    assert row.body == {"item_type": "message", "backend_item_id": "msg_1"}


def test_the_three_kinds_say_where_in_a_lifecycle_a_row_sits_and_the_body_says_of_what() -> None:
    """Which shape a body has follows from the item's type, not from which of the three events it is
    — so the kind is a position and `item_type` is what a reader dispatches on."""
    rows = [
        stored(ReasoningStarted(provenance=WHERE)),
        stored(ItemSegment(item=OpenRef(item_type=ItemType.REASONING), text="thinking", provenance=WHERE)),
        stored(
            ToolCallCompleted(
                item=CallRef(call_id="toolu_1"), structured=None, outcome=ToolOutcome.UNKNOWN, provenance=WHERE
            )
        ),
    ]

    assert [row.kind for row in rows] == [
        ConversationEventKind.ITEM_STARTED,
        ConversationEventKind.ITEM_SEGMENT,
        ConversationEventKind.ITEM_COMPLETED,
    ]
    assert [row.body.get("item_type") for row in rows] == ["reasoning", None, "tool_call"]


def test_the_provenance_arm_follows_from_whether_frames_were_named() -> None:
    """The two arms of an item row, which is what separates an item kind from an authored one: both
    are legal here, and which one a row takes follows from the item rather than from the kind.

    That a *frame-derived* event carrying `Authored` is refused rather than written on the second
    arm is the log writer's guard, tested where it lives — a row written that way would fail on the
    read, taking a whole conversation's transcript with it.
    """
    folded = stored(ReasoningStarted(provenance=WHERE))
    authored = session_events.item_row(
        ConversationEventKind.ITEM_STARTED,
        session_events.PromptStartedBody(origin=SPA_ORIGIN),
        conversation_id=CONVERSATION_ID,
        event_seq=1,
        item_id=ITEM_ID,
        session_id=SESSION_ID,
        turn_id=None,
        provenance=None,
        now=NOW,
    )

    assert (folded.provenance, authored.provenance) == (EventProvenance.FRAME_RANGE, EventProvenance.AUTHORED)


def test_a_tool_call_and_its_answer_are_two_rows_and_neither_carries_the_output() -> None:
    """The prose a call printed is a segment like any other item's, so the completion holds only
    what no string carries."""
    started = stored(
        ToolCallStarted(call_id="toolu_1", tool_name="Bash", arguments={"command": "ls"}, provenance=WHERE)
    )
    said = stored(ItemSegment(item=CallRef(call_id="toolu_1"), text="a.py\nb.py", provenance=FrameRange(15, 15)))
    completed = stored(
        ToolCallCompleted(
            item=CallRef(call_id="toolu_1"),
            structured={"exit_code": 0},
            outcome=ToolOutcome.SUCCEEDED,
            provenance=FrameRange(15, 15),
        )
    )

    assert started.body == {
        "item_type": "tool_call",
        "call_id": "toolu_1",
        "tool_name": "Bash",
        "arguments": {"command": "ls"},
    }
    assert said.body == {"text": "a.py\nb.py"}
    assert completed.body == {"item_type": "tool_call", "structured": {"exit_code": 0}, "outcome": "succeeded"}


def test_a_prompt_is_conversation_on_the_authored_arm() -> None:
    """The operator's half of the transcript, which no fold produces: a prompt is accepted before it
    crosses the wire, so it has frames nowhere and a turn not yet."""
    asked = session_events.item_row(
        ConversationEventKind.ITEM_STARTED,
        session_events.PromptStartedBody(origin=SPA_ORIGIN),
        conversation_id=CONVERSATION_ID,
        event_seq=1,
        item_id=ITEM_ID,
        session_id=SESSION_ID,
        turn_id=None,
        provenance=None,
        now=NOW,
    )

    assert (asked.kind, asked.provenance) == (ConversationEventKind.ITEM_STARTED, EventProvenance.AUTHORED)
    assert (asked.turn_id, asked.source_first_frame_seq, asked.source_last_frame_seq) == (None,) * 3
    assert asked.body == {"item_type": "prompt", "origin": {"kind": "spa"}}


def test_a_room_prompt_names_the_room_as_well_as_the_events() -> None:
    """A bare event id cannot tell a sibling room's copy of a prompt from this room's, which is
    exactly the question the surface reading this asks. Both strings stay the channel's own."""
    origin = MatrixOrigin(address="!room:example.org", refs=("$a", "$b"))

    assert session_events.PromptStartedBody(origin=origin).model_dump(mode="json")["origin"] == {
        "kind": "matrix",
        "address": "!room:example.org",
        "refs": ["$a", "$b"],
    }


def test_a_prompt_body_without_an_origin_is_rejected() -> None:
    """The reason the SPA is a named arm rather than the absent one: there is no default to fall
    back to, because reading a missing key as "typed into a browser" would tell every attached room
    it owes a copy of a prompt the room may already be showing."""
    with pytest.raises(ValidationError):
        session_events.PromptStartedBody.model_validate({"item_type": "prompt"})


def test_a_fact_the_console_authored_names_no_item_and_no_frames() -> None:
    """The second category: what happened *to* the session. It crossed no wire, and it is the
    conversation's fact rather than an item's — which is what lets a session that never reached a
    turn have a stream at all."""
    taken = session_events.authored(
        session_events.SessionAdoptedBody(previous_holder="haku-console-a", holder="haku-console-b"),
        conversation_id=CONVERSATION_ID,
        event_seq=3,
        session_id=SESSION_ID,
        turn_id=None,
        now=NOW,
    )

    assert (taken.kind, taken.provenance) == (AuthoredEventKind.SESSION_ADOPTED, EventProvenance.AUTHORED)
    assert (taken.turn_id, taken.item_id, taken.source_first_frame_seq, taken.source_last_frame_seq) == (None,) * 4
    assert taken.body == {"previous_holder": "haku-console-a", "holder": "haku-console-b"}


def test_the_kind_of_an_authored_row_follows_from_its_body() -> None:
    """One row per fact and no way to label it as another: the body is the discriminator."""
    lapsed = session_events.authored(
        session_events.LeaseExpiredBody(reason=LeaseExpiryReason.UNADOPTED, last_holder=None),
        conversation_id=CONVERSATION_ID,
        event_seq=4,
        session_id=SESSION_ID,
        turn_id=None,
        now=NOW,
    )

    assert lapsed.kind == AuthoredEventKind.LEASE_EXPIRED
    assert lapsed.body == {"reason": "unadopted", "last_holder": None}


# One stored body per kind the column may hold. A kind added without an entry fails
# `test_every_kind_the_column_may_hold_can_be_read_back` rather than reaching a replica that
# cannot parse it. The three item kinds hold one body per item type, since that is where a body's
# shape actually comes from.
_BODIES: dict[StoredEventKind, list[dict[str, object]]] = {
    ConversationEventKind.ITEM_STARTED: [
        {"item_type": "message"},
        {"item_type": "reasoning"},
        {"item_type": "tool_call", "call_id": "toolu_1", "tool_name": "Bash", "arguments": {"command": "ls"}},
        {"item_type": "prompt", "origin": {"kind": "spa"}},
    ],
    ConversationEventKind.ITEM_SEGMENT: [{"text": "a run of prose"}],
    ConversationEventKind.ITEM_COMPLETED: [
        {"item_type": "message", "backend_item_id": "msg_1"},
        {"item_type": "reasoning", "disclosure": ReasoningDisclosure.WITHHELD},
        {"item_type": "tool_call", "structured": {"exit_code": 0}, "outcome": "succeeded"},
        {"item_type": "prompt"},
    ],
    AuthoredEventKind.PROMPT_REJECTED: [{"reason": "turn_in_flight", "text": "wait"}],
    AuthoredEventKind.UNREADABLE_INPUT: [{"media_type": "m.image"}],
    AuthoredEventKind.SESSION_ADOPTED: [{"previous_holder": None, "holder": "haku-console-a"}],
    AuthoredEventKind.LEASE_EXPIRED: [{"reason": "unadopted", "last_holder": None}],
    AuthoredEventKind.TURN_STARTED: [{}],
    AuthoredEventKind.TURN_ENDED: [{"outcome": "answered"}],
    AuthoredEventKind.SESSION_PROVISIONING: [{}],
    AuthoredEventKind.SESSION_ENDED: [{"status": "failed", "error": "the claim was never satisfied"}],
    AuthoredEventKind.SETUP_NARRATION: [{"text": "cloning haku-state"}],
}


def test_every_kind_the_column_may_hold_can_be_read_back() -> None:
    """The property a roll depends on, which is stronger than the type check behind it.

    Tolerance covers the kind a *later* release adds; it cannot cover one this release already
    holds and has no body for, because `body_of` would then be reading a kind it does name. So this
    says the vocabulary is complete — every kind the column may hold parses — which is what lets a
    writer for these ship without a second release.
    """
    unreadable = [
        (kind, body)
        for kind in (*ConversationEventKind, *AuthoredEventKind)
        for body in _BODIES[kind]
        if session_events.body_of(
            ConversationEventRow(kind=kind, body=body, created_at=NOW, provenance=EventProvenance.AUTHORED)
        )
        is None
    ]

    assert unreadable == []


def test_a_row_of_a_kind_this_release_has_no_words_for_reads_as_one() -> None:
    """What the previous image sees for the length of a roll once a release adds a kind.

    The point is that it is a value: the read that produced it carries every other row of that
    conversation, and a subscriber can position and skip this one instead of losing all of them.
    """
    row = ConversationEventRow(
        conversation_id=CONVERSATION_ID,
        session_id=SESSION_ID,
        turn_id=None,
        item_id=None,
        kind=UnknownValue("provisioning_started"),
        provenance=EventProvenance.AUTHORED,
        source_first_frame_seq=None,
        source_last_frame_seq=None,
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
    row = ConversationEventRow(
        conversation_id=CONVERSATION_ID,
        session_id=SESSION_ID,
        turn_id=None,
        item_id=None,
        kind=AuthoredEventKind.LEASE_EXPIRED,
        provenance=EventProvenance.AUTHORED,
        source_first_frame_seq=None,
        source_last_frame_seq=None,
        body={"reason": "unadopted", "last_holder": None, "swept_by": "a field added later"},
        created_at=NOW,
    )

    assert session_events.body_of(row) == session_events.LeaseExpiredBody(
        reason=LeaseExpiryReason.UNADOPTED, last_holder=None
    )


def test_the_one_event_with_a_durable_home_elsewhere_gets_no_row() -> None:
    """A turn's ending is the `conversation_turn` row, plus the two authored rows `end_turn` writes
    to state it in the stream."""
    assert session_events.stored(TurnCompleted(end=TurnAnswered(), provenance=WHERE)) is None


if __name__ == "__main__":
    pytest_bazel.main()
