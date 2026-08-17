"""What one `/sync` yields the loop: which events count as input, and how a gap is closed."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import pytest_bazel
from nio.responses import RoomMessagesResponse, SyncResponse

# Aliased: `client` is the name every test here gives its `MatrixClient` local.
from haku.console.x.channels.matrix import client as matrix_client
from haku.console.x.channels.matrix.client import EventTag, MatrixClient, RoomEventKind

USER = "@haku:allegedly.works"
OPERATOR = "@rai:allegedly.works"
ROOM = "!room:allegedly.works"


def _message(sender: str, body: str, event_id: str = "$evt", msgtype: str = "m.text") -> dict[str, Any]:
    return {
        "type": "m.room.message",
        "event_id": event_id,
        "sender": sender,
        "origin_server_ts": 1,
        "content": {"msgtype": msgtype, "body": body},
    }


def _sync_body(*events: dict[str, Any], limited: bool = False, next_batch: str = "s2") -> dict[str, Any]:
    timeline = {"events": list(events), "limited": limited, "prev_batch": "p1"}
    return {"next_batch": next_batch, "rooms": {"join": {ROOM: {"timeline": timeline}}}}


def _invite_body(state_key: str, inviter: str = OPERATOR, room_id: str = "!new:allegedly.works") -> dict[str, Any]:
    member = {"type": "m.room.member", "state_key": state_key, "sender": inviter, "content": {"membership": "invite"}}
    return {"next_batch": "s3", "rooms": {"invite": {room_id: {"invite_state": {"events": [member]}}}}}


@dataclass
class _Homeserver:
    """Canned `/sync` and `/messages` responses, and a record of how they were asked for."""

    sync_body: dict[str, Any]
    pages: list[dict[str, Any]] = field(default_factory=list)
    sync_kwargs: dict[str, Any] = field(default_factory=dict)
    message_kwargs: dict[str, Any] = field(default_factory=dict)

    async def sync(self, **kwargs: Any) -> SyncResponse:
        self.sync_kwargs = kwargs
        response = SyncResponse.from_dict(self.sync_body)
        assert isinstance(response, SyncResponse), response
        return response

    async def room_messages(self, room_id: str, **kwargs: Any) -> RoomMessagesResponse:
        self.message_kwargs = kwargs
        page = self.pages.pop(0) if self.pages else {"chunk": [], "start": "x"}
        response = RoomMessagesResponse.from_dict(page, room_id)
        assert isinstance(response, RoomMessagesResponse), response
        return response


def _client(sync_body: dict[str, Any], pages: list[dict[str, Any]] | None = None) -> tuple[MatrixClient, _Homeserver]:
    """A client whose homeserver calls are canned, so parsing is what's under test."""
    client = MatrixClient("https://matrix.allegedly.works", USER, "haku-console")
    homeserver = _Homeserver(sync_body, list(pages or []))
    client._client.sync = homeserver.sync
    client._client.room_messages = homeserver.room_messages
    return client, homeserver


async def test_parses_an_operator_message():
    client, _ = _client(_sync_body(_message(OPERATOR, "hello")))

    result = await client.sync("tok", since="s1")

    assert result.next_batch == "s2"
    [message] = result.messages
    assert (message.sender, message.body, message.room_id) == (OPERATOR, "hello", ROOM)


async def test_skips_our_own_messages():
    """The bot's own posts come back through /sync and are not input."""
    client, _ = _client(_sync_body(_message(USER, "echo: hello")))

    result = await client.sync("tok", since="s1")

    assert result.messages == ()


async def test_an_image_is_reported_rather_than_dropped():
    """Haku cannot read an attachment, but the operator has to learn that it tried."""
    client, _ = _client(_sync_body(_message(OPERATOR, "photo.png", event_id="$img", msgtype="m.image")))

    result = await client.sync("tok", since="s1")

    assert result.messages == ()
    [unreadable] = result.unmappable
    assert (unreadable.event_id, unreadable.sender, unreadable.msgtype, unreadable.room_id) == (
        "$img",
        OPERATOR,
        "m.image",
        ROOM,
    )


async def test_an_msgtype_this_release_has_never_heard_of_is_reported_too():
    """The point of reporting by msgtype rather than by nio's class: an extensible-events
    msgtype nio parses as `RoomMessageUnknown` is exactly as unreadable as an image."""
    client, _ = _client(_sync_body(_message(OPERATOR, "?", msgtype="works.allegedly.hologram")))

    result = await client.sync("tok", since="s1")

    assert [event.msgtype for event in result.unmappable] == ["works.allegedly.hologram"]


async def test_an_emote_is_read_as_prose():
    """`m.emote` is `m.text` in the third person — the words are in `body`, so there is nothing
    to fail to map and nothing to report."""
    client, _ = _client(_sync_body(_message(OPERATOR, "waves at Haku", msgtype="m.emote")))

    result = await client.sync("tok", since="s1")

    assert [message.body for message in result.messages] == ["waves at Haku"]
    assert result.unmappable == ()


async def test_a_notice_produces_neither_a_message_nor_a_report():
    """The loop guard. Everything Haku says that is not an answer is an `m.notice`, including the
    notice it posts *about* an unreadable event, so a notice must never be reportable from any
    sender. The sender rule excludes the bot's own; this covers the case where that is not what
    saves us."""
    client, _ = _client(
        _sync_body(
            _message(USER, "received 1 message(s) Haku cannot read", event_id="$ours", msgtype="m.notice"),
            _message(OPERATOR, "some other bot", event_id="$theirs", msgtype="m.notice"),
        )
    )

    result = await client.sync("tok", since="s1")

    assert (result.messages, result.unmappable) == ((), ())


async def test_our_own_image_is_not_reported():
    """Sender first, mapping second: an attachment Haku itself posted is not an event to tell the
    operator about."""
    client, _ = _client(_sync_body(_message(USER, "chart.png", msgtype="m.image")))

    result = await client.sync("tok", since="s1")

    assert (result.messages, result.unmappable) == ((), ())


async def test_skips_non_message_events():
    """Membership, topic and reactions are not messages, so there is nothing unmapped about them
    and the operator must not be told anything happened."""
    topic = {"type": "m.room.topic", "event_id": "$t", "sender": OPERATOR, "origin_server_ts": 1, "content": {}}
    reaction = {
        "type": "m.reaction",
        "event_id": "$r",
        "sender": OPERATOR,
        "origin_server_ts": 1,
        "content": {"m.relates_to": {"rel_type": "m.annotation", "event_id": "$evt", "key": "👍"}},
    }
    member = {
        "type": "m.room.member",
        "event_id": "$m",
        "state_key": OPERATOR,
        "sender": OPERATOR,
        "origin_server_ts": 1,
        "content": {"membership": "join"},
    }
    client, _ = _client(_sync_body(topic, reaction, member))

    result = await client.sync("tok", since="s1")

    assert (result.messages, result.unmappable) == ((), ())


async def test_a_redacted_message_is_not_reported():
    """A redaction keeps the type and loses the content. There is nothing left to read, and
    telling the operator Haku could not read the message they just deleted is noise."""
    redacted = {
        "type": "m.room.message",
        "event_id": "$gone",
        "sender": OPERATOR,
        "origin_server_ts": 1,
        "content": {},
        "unsigned": {"redacted_because": {"type": "m.room.redaction", "sender": OPERATOR}},
    }
    client, _ = _client(_sync_body(redacted))

    result = await client.sync("tok", since="s1")

    assert (result.messages, result.unmappable) == ((), ())


async def test_the_text_of_a_mixed_batch_still_arrives():
    """A "look at this" alongside a screenshot: the sentence is serviceable and must not be held
    hostage by the attachment next to it."""
    client, _ = _client(
        _sync_body(
            _message(OPERATOR, "look at this", event_id="$a"),
            _message(OPERATOR, "screenshot.png", event_id="$b", msgtype="m.image"),
        )
    )

    result = await client.sync("tok", since="s1")

    assert [message.body for message in result.messages] == ["look at this"]
    assert [event.event_id for event in result.unmappable] == ["$b"]


async def test_a_gap_full_of_attachments_is_recovered_as_reportable():
    """Downtime recovery's backfill has the same filter as the live timeline, so it had the same hole."""
    client, _ = _client(
        _sync_body(limited=True),
        pages=[
            {"chunk": [_message(OPERATOR, "memo.ogg", event_id="$v", msgtype="m.audio")], "start": "p1", "end": "p2"},
            {"chunk": [], "start": "p2"},
        ],
    )

    result = await client.sync("tok", since="s1")

    assert [event.msgtype for event in result.unmappable] == ["m.audio"]


async def test_parses_an_invite_and_its_sender():
    client, _ = _client(_invite_body(USER))

    [invite] = (await client.sync("tok", since="s1")).invites

    assert (invite.room_id, invite.inviter) == ("!new:allegedly.works", OPERATOR)


async def test_ignores_membership_events_about_other_users():
    """Somebody else being invited to a room we are in is not an invite to us."""
    client, _ = _client(_invite_body("@someone:allegedly.works"))

    result = await client.sync("tok", since="s1")

    assert result.invites == ()


async def test_empty_sync_still_yields_the_watermark():
    client, _ = _client({"next_batch": "s9"})

    result = await client.sync("tok", since="s1")

    assert (result.next_batch, result.messages, result.invites) == ("s9", (), ())


async def test_backfills_a_truncated_timeline_in_order():
    """A `limited` timeline is a gap; without paginating it, downtime silently eats messages."""
    client, homeserver = _client(
        _sync_body(_message(OPERATOR, "third", event_id="$c"), limited=True),
        pages=[
            # /messages paginates backwards, so the newest of the missed events comes first.
            {
                "chunk": [_message(OPERATOR, "second", event_id="$b"), _message(OPERATOR, "first", event_id="$a")],
                "start": "p1",
                "end": "p2",
            },
            # Synapse omits `end` once the requested range is exhausted.
            {"chunk": [], "start": "p2"},
        ],
    )

    result = await client.sync("tok", since="s1")

    assert [message.body for message in result.messages] == ["first", "second", "third"]
    assert homeserver.message_kwargs["end"] == "s1", "backfill must stop at the watermark, not walk all history"


async def test_first_sync_takes_a_position_without_replaying_backlog():
    """No watermark means no missed range — only a starting point (and any pending invite)."""
    client, homeserver = _client(_sync_body(limited=True))

    result = await client.sync("tok", since=None)

    assert result.messages == ()
    assert homeserver.sync_kwargs["sync_filter"] == {"room": {"timeline": {"limit": 0}}}
    assert homeserver.message_kwargs == {}, "a truncated first sync is not a gap and must not be backfilled"


async def test_backfill_is_bounded_and_says_what_it_dropped(monkeypatch, caplog):
    """A room busy for a week must not stall the loop — but a truncated recovery is never silent."""
    monkeypatch.setattr(matrix_client, "MAX_BACKFILL_PAGES", 2)
    page = {"chunk": [_message(OPERATOR, "endless")], "start": "p", "end": "p"}
    client, _ = _client(_sync_body(limited=True), pages=[page, page, page, page])

    with caplog.at_level("ERROR"):
        result = await client.sync("tok", since="s1")

    assert len(result.messages) == 2
    assert "gave up backfilling" in caplog.text


def test_a_tag_is_ids_and_kinds_and_omits_what_it_has_none_of() -> None:
    """What goes under the console's own content key. Nothing reads one back, so what this pins is
    the wire, for the room's readers."""
    tag = EventTag(
        kind=RoomEventKind.REPLY,
        session_id=UUID("11111111-2222-3333-4444-555555555555"),
        message_id=UUID("99999999-8888-7777-6666-555555555555"),
    )

    assert tag.content() == {
        "kind": "reply",
        "session_id": "11111111-2222-3333-4444-555555555555",
        "message_id": "99999999-8888-7777-6666-555555555555",
    }


if __name__ == "__main__":
    pytest_bazel.main()
