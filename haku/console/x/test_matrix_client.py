"""What one `/sync` yields the loop: which events count as input, and how a gap is closed."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest_bazel
from nio.responses import RoomMessagesResponse, SyncResponse

from haku.console.x import matrix_client
from haku.console.x.matrix_client import MatrixClient

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
    """R1.5 — the bot's own posts come back through /sync and are not input."""
    client, _ = _client(_sync_body(_message(USER, "echo: hello")))

    result = await client.sync("tok", since="s1")

    assert result.messages == ()


async def test_skips_non_text_messages():
    client, _ = _client(_sync_body(_message(OPERATOR, "photo.png", msgtype="m.image")))

    result = await client.sync("tok", since="s1")

    assert result.messages == ()


async def test_skips_non_message_events():
    topic = {"type": "m.room.topic", "event_id": "$t", "sender": OPERATOR, "origin_server_ts": 1, "content": {}}
    client, _ = _client(_sync_body(topic))

    result = await client.sync("tok", since="s1")

    assert result.messages == ()


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
    """R1.7 — a `limited` timeline is a gap; without paginating it, downtime silently eats messages."""
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


if __name__ == "__main__":
    pytest_bazel.main()
