"""Sync-response parsing: what the loop is allowed to act on."""

from __future__ import annotations

import httpx
import pytest_bazel

from haku.console.matrix_client import MatrixClient

USER = "@haku:allegedly.works"
OPERATOR = "@rai:allegedly.works"


def _client() -> MatrixClient:
    return MatrixClient(httpx.AsyncClient(), "https://matrix.allegedly.works", USER)


def _joined(*events: dict) -> dict:
    return {"next_batch": "s2", "rooms": {"join": {"!room:allegedly.works": {"timeline": {"events": list(events)}}}}}


def _message(sender: str, body: str, event_id: str = "$evt", msgtype: str = "m.text") -> dict:
    return {
        "type": "m.room.message",
        "event_id": event_id,
        "sender": sender,
        "origin_server_ts": 1,
        "content": {"msgtype": msgtype, "body": body},
    }


def test_parses_an_operator_message():
    result = _client()._parse_sync(_joined(_message(OPERATOR, "hello")))

    assert result.next_batch == "s2"
    [message] = result.messages
    assert (message.sender, message.body, message.room_id) == (OPERATOR, "hello", "!room:allegedly.works")


def test_skips_our_own_messages():
    """R1.5 — the bot's own posts come back through /sync and are not input."""
    result = _client()._parse_sync(_joined(_message(USER, "echo: hello")))

    assert result.messages == ()


def test_skips_non_text_messages():
    result = _client()._parse_sync(_joined(_message(OPERATOR, "photo.png", msgtype="m.image")))

    assert result.messages == ()


def test_skips_non_message_events():
    result = _client()._parse_sync(_joined({"type": "m.room.topic", "event_id": "$t", "sender": OPERATOR}))

    assert result.messages == ()


def test_parses_an_invite_and_its_sender():
    body = {
        "next_batch": "s3",
        "rooms": {
            "invite": {
                "!new:allegedly.works": {
                    "invite_state": {
                        "events": [
                            {
                                "type": "m.room.member",
                                "state_key": USER,
                                "sender": OPERATOR,
                                "content": {"membership": "invite"},
                            }
                        ]
                    }
                }
            }
        },
    }

    [invite] = _client()._parse_sync(body).invites

    assert (invite.room_id, invite.inviter) == ("!new:allegedly.works", OPERATOR)


def test_ignores_membership_events_about_other_users():
    """Somebody else being invited to a room we are in is not an invite to us."""
    body = {
        "next_batch": "s3",
        "rooms": {
            "invite": {
                "!new:allegedly.works": {
                    "invite_state": {
                        "events": [
                            {
                                "type": "m.room.member",
                                "state_key": "@someone:allegedly.works",
                                "sender": OPERATOR,
                                "content": {"membership": "invite"},
                            }
                        ]
                    }
                }
            }
        },
    }

    assert _client()._parse_sync(body).invites == ()


def test_empty_sync_still_yields_the_watermark():
    result = _client()._parse_sync({"next_batch": "s9"})

    assert (result.next_batch, result.messages, result.invites) == ("s9", (), ())


if __name__ == "__main__":
    pytest_bazel.main()
