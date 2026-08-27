"""Contracts of the room's durable copy: correspondence, redaction visibility, duplicate repair.

Against real Postgres, like every store here — the foreign key to `chat_attachment` and the
conflict behaviour under re-recording are exactly what a fake would agree with the code about.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
import pytest_bazel

from haku.console.x.channels.matrix.client import ConversationEventSource, ProjectedEvent, Redaction
from haku.console.x.channels.matrix.conftest import MATRIX_ROOM
from haku.console.x.channels.matrix.conversation import MatrixConversationStore
from haku.console.x.channels.matrix.room_copy import RedundantCopy, RoomCopy


@pytest.fixture
def copy(migrated_sessions) -> RoomCopy:
    return RoomCopy(migrated_sessions)


@pytest.fixture
async def attached(conversations: MatrixConversationStore, operator_id: UUID) -> tuple[UUID, UUID]:
    """A bound room's conversation and attachment, which recorded echoes must exist under."""
    bound = await conversations.bind_room(MATRIX_ROOM, operator_id)
    attachment_id = await conversations.attachment(MATRIX_ROOM)
    assert attachment_id is not None
    return bound.conversation_id, attachment_id


def _echo(event_id: str, attached: tuple[UUID, UUID], seq: int, ts: int, replaces: str | None = None) -> ProjectedEvent:
    conversation_id, attachment_id = attached
    return ProjectedEvent(
        room_id=MATRIX_ROOM,
        event_id=event_id,
        source=ConversationEventSource(attachment_id=attachment_id, conversation_id=conversation_id, event_seq=seq),
        origin_server_ts=ts,
        replaces_event_id=replaces,
    )


async def test_a_recorded_echo_answers_shows(copy, attached) -> None:
    _, attachment_id = attached

    assert await copy.record([_echo("$e1", attached, seq=7, ts=1)], []) == ()

    assert await copy.shows(attachment_id, 7)
    assert not await copy.shows(attachment_id, 8)
    assert not await copy.shows(uuid4(), 7)


async def test_re_recording_an_echo_changes_nothing(copy, attached) -> None:
    """A crash between recording and the watermark re-records the same batch."""
    _, attachment_id = attached
    echo = _echo("$e1", attached, seq=7, ts=1)

    assert await copy.record([echo], []) == ()
    assert await copy.record([echo], []) == ()

    assert await copy.shows(attachment_id, 7)


async def test_a_second_live_copy_is_the_one_reported(copy, attached) -> None:
    """The duplicate a replay past the transaction cache leaves: the later copy is redundant."""
    assert await copy.record([_echo("$first", attached, seq=7, ts=1)], []) == ()

    assert await copy.record([_echo("$again", attached, seq=7, ts=2)], []) == (
        RedundantCopy(attachment_id=attached[1], event_id="$again"),
    )


async def test_the_earliest_copy_is_kept_whatever_order_the_echoes_arrive(copy, attached) -> None:
    """Backfill can show the copies newest-first; the room's own order decides, not ours."""
    assert await copy.record([_echo("$later", attached, seq=7, ts=2)], []) == ()

    assert await copy.record([_echo("$earliest", attached, seq=7, ts=1)], []) == (
        RedundantCopy(attachment_id=attached[1], event_id="$later"),
    )


async def test_sources_are_independent(copy, attached) -> None:
    assert await copy.record([_echo("$a", attached, seq=7, ts=1), _echo("$b", attached, seq=8, ts=2)], []) == ()


async def test_an_edit_satisfies_correspondence_but_is_never_a_duplicate(copy, attached) -> None:
    """An `m.replace` revises the copy the room already shows; redacting it would be repair
    un-editing a newer release's reconciliation."""
    _, attachment_id = attached
    await copy.record([_echo("$original", attached, seq=7, ts=1)], [])

    assert await copy.record([_echo("$edit", attached, seq=7, ts=2, replaces="$original")], []) == ()
    assert await copy.shows(attachment_id, 7)


async def test_a_redacted_copy_is_out_of_repair_but_still_shows(copy, attached) -> None:
    """Redaction visibility, in both of its roles.

    For repair, a redacted event reads as absence: once the duplicate is taken back the pair stops
    being reported. For correspondence, the row remains an answer: the source reached the room
    once, and a cursor replay must not re-post what the operator unsaid.
    """
    _, attachment_id = attached
    await copy.record([_echo("$first", attached, seq=7, ts=1)], [])
    assert await copy.record([_echo("$again", attached, seq=7, ts=2)], []) == (
        RedundantCopy(attachment_id=attachment_id, event_id="$again"),
    )

    assert await copy.record([], [Redaction(room_id=MATRIX_ROOM, redacts_event_id="$again")]) == ()

    # Re-observing the surviving copy reveals no duplicate any more.
    assert await copy.record([_echo("$first", attached, seq=7, ts=1)], []) == ()

    await copy.record([], [Redaction(room_id=MATRIX_ROOM, redacts_event_id="$first")])
    assert await copy.shows(attachment_id, 7), "a redaction takes back the copy, not the correspondence"


async def test_a_redaction_arriving_with_its_target_wins(copy, attached) -> None:
    """One batch can carry an event and the redaction that unsays it; the room's final word is
    what the store keeps."""
    await copy.record([_echo("$first", attached, seq=7, ts=1)], [])

    assert (
        await copy.record(
            [_echo("$again", attached, seq=7, ts=2)], [Redaction(room_id=MATRIX_ROOM, redacts_event_id="$again")]
        )
        == ()
    )


async def test_a_redelivered_echo_does_not_resurrect_a_redaction(copy, attached) -> None:
    """Gap recovery can hand the same event back after its redaction was recorded."""
    await copy.record([_echo("$first", attached, seq=7, ts=1)], [])
    await copy.record([_echo("$again", attached, seq=7, ts=2)], [])
    await copy.record([], [Redaction(room_id=MATRIX_ROOM, redacts_event_id="$again")])

    assert await copy.record([_echo("$again", attached, seq=7, ts=2)], []) == ()


async def test_a_redaction_of_an_event_never_recorded_is_a_no_op(copy, attached) -> None:
    """The operator redacting their own message reaches the store as a target it never held."""
    assert await copy.record([], [Redaction(room_id=MATRIX_ROOM, redacts_event_id="$not-ours")]) == ()


async def test_an_echo_tagged_with_a_dropped_attachment_is_skipped_loudly(copy, attached, caplog) -> None:
    """Room events are permanent and conversation data may be dropped wholesale, so an event tagged
    with an attachment the database no longer holds must degrade to a logged skip — never wedge the
    sync loop on a foreign key it can never satisfy, and never take the batch's readable rows with
    it."""
    conversation_id, attachment_id = attached
    dropped = ProjectedEvent(
        room_id=MATRIX_ROOM,
        event_id="$orphan",
        source=ConversationEventSource(attachment_id=uuid4(), conversation_id=conversation_id, event_seq=3),
        origin_server_ts=1,
        replaces_event_id=None,
    )

    with caplog.at_level("WARNING"):
        assert await copy.record([dropped, _echo("$kept", attached, seq=7, ts=2)], []) == ()

    assert "no longer exists" in caplog.text
    assert await copy.shows(attachment_id, 7)
    assert not await copy.shows(dropped.source.attachment_id, 3)


if __name__ == "__main__":
    pytest_bazel.main()
