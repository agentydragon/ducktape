"""The operator's side of a room: what a person sitting in it can do, and what they see.

**Deliberately not built on `matrix_client.MatrixClient`.** That client is Haku's side — the thing
under test, the one that decides what the console says and how the console reads what came back.
A test that drove it from both ends would agree with itself about any bug in it: a message the
client never sent and a reading that skipped it look identical. So this is a second, independent
account of the same room, taken straight off the client-server API, and it has to stay one.
Reaching for the production client here to save the parsing below would quietly delete the
property every test in this package rests on.

What the room hands back is a `RoomEvent`, not the wire's JSON. The three shapes the console
actually produces are three variants — a message, an edit that replaces one, and a message a
redaction has emptied — so "is this a reply or the status line being rewritten?" is an
`isinstance`, not a check for a key whose absence means something.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from haku.console.x.testing.synapse_container import Account
from haku.console.x.testing.waiting import WedgedError, wait_until

# How long an ephemeral event may take to reach the other side of the room.
_TYPING_BUDGET_SECONDS = 10.0


class MessageKind(StrEnum):
    """The `msgtype` of a message these rooms carry.

    Strict: a msgtype outside this set is a console that started saying something new, which a
    test should hear about rather than silently classify as "not a reply".
    """

    TEXT = "m.text"
    NOTICE = "m.notice"
    EMOTE = "m.emote"
    IMAGE = "m.image"


@dataclass(frozen=True)
class Message:
    """Something said in the room, whole.

    `formatted_body` is None for a message sent as plain text — a client that renders HTML has
    nothing to render, rather than an empty rendering.
    """

    event_id: str
    sender: str
    kind: MessageKind
    body: str
    formatted_body: str | None


@dataclass(frozen=True)
class Edit:
    """A replacement of an earlier message (`m.replace`), which is how the status line changes.

    `body` is the `* new text` fallback a client that ignores edits shows; `new_body` is what a
    client that honours them renders instead. `content` is the raw half, kept for assertions about
    keys this API deliberately does not model — the console's `works.allegedly.haku` tag rides in
    there, and a person in the room does not see it.
    """

    event_id: str
    sender: str
    replaces: str
    body: str
    new_body: str
    content: dict[str, Any]


@dataclass(frozen=True)
class Redacted:
    """A message the room still lists and whose content is gone."""

    event_id: str
    sender: str
    reason: str | None


@dataclass(frozen=True)
class Redaction:
    """Somebody taking a message back."""

    event_id: str
    sender: str
    redacts: str


@dataclass(frozen=True)
class StateChange:
    """Everything a room is besides what was said in it: who created it, who joined, who may talk."""

    event_id: str
    sender: str
    event_type: str


RoomEvent = Message | Edit | Redacted | Redaction | StateChange


def _parse(event: dict[str, Any]) -> RoomEvent:
    event_id, sender, content = event["event_id"], event["sender"], event["content"]
    match event["type"]:
        case "m.room.redaction":
            # `redacts` moved from the event into its content in room version 11.
            return Redaction(event_id=event_id, sender=sender, redacts=content.get("redacts") or event["redacts"])
        case "m.room.message":
            if (because := event.get("unsigned", {}).get("redacted_because")) is not None:
                # Checked before the content is read: a redaction leaves none to read.
                return Redacted(event_id=event_id, sender=sender, reason=because["content"].get("reason"))
            if (new_content := content.get("m.new_content")) is not None:
                return Edit(
                    event_id=event_id,
                    sender=sender,
                    replaces=content["m.relates_to"]["event_id"],
                    body=content["body"],
                    new_body=new_content["body"],
                    content=content,
                )
            return Message(
                event_id=event_id,
                sender=sender,
                kind=MessageKind(content["msgtype"]),
                body=content["body"],
                formatted_body=content.get("formatted_body"),
            )
        case event_type:
            return StateChange(event_id=event_id, sender=sender, event_type=event_type)


def _parse_edit(event: dict[str, Any]) -> Edit:
    if not isinstance(parsed := _parse(event), Edit):
        raise ValueError(f"the homeserver indexed a non-edit as one: {event=}")
    return parsed


class OperatorRoom:
    """One room, driven and read as the operator.

    *check_alive* is handed to every wait: a test whose console has died fails at the death rather
    than at the deadline. Omitted where nothing but the homeserver is serving the room.
    """

    def __init__(
        self, account: Account, *, bot_user_id: str, room_id: str, check_alive: Callable[[], None] | None = None
    ):
        self.room_id = room_id
        self._account = account
        self._bot = bot_user_id
        self._check_alive = check_alive
        self._typing_watermark: str | None = None

    def say(self, body: str) -> str:
        return self._account.send_text(self.room_id, body)

    def send(self, content: dict[str, Any]) -> str:
        """Put an arbitrary `m.room.message` in the room.

        The escape hatch for what a person's client sends and this API does not model — an image,
        with the `url` that makes it one. What comes back is still read as a `RoomEvent`.
        """
        return self._account.send(self.room_id, content)

    def timeline(self) -> list[RoomEvent]:
        """Everything in the room, oldest first."""
        return [_parse(event) for event in reversed(self._account.messages(self.room_id))]

    def event(self, event_id: str) -> RoomEvent:
        return _parse(self._account.event(self.room_id, event_id))

    def content_of(self, event_id: str) -> dict[str, Any]:
        """The event's `content` off the wire, for the keys this API deliberately does not model.

        The console's `works.allegedly.haku` tag rides in there, and a person sitting in the room
        does not see it — so it is not a field of `Message`, and a test that is about the tag
        surviving the homeserver reads it from here. The same half `Edit.content` carries.
        """
        content: dict[str, Any] = self._account.event(self.room_id, event_id)["content"]
        return content

    def edits_of(self, event_id: str) -> list[Edit]:
        """The homeserver's own index of what replaced *event_id*, rather than our reading of the timeline."""
        return [_parse_edit(event) for event in self._account.relations(self.room_id, event_id, "m.replace")]

    def replies(self) -> list[str]:
        """What Haku said into the room, oldest first.

        `m.text` only: everything the console says *about* the conversation — joining, sandbox
        narration, the status line, lifecycle — is an `m.notice`, and a rewritten one is an `Edit`
        rather than a message of its own.
        """
        return [
            event.body
            for event in self.timeline()
            if isinstance(event, Message) and event.sender == self._bot and event.kind is MessageKind.TEXT
        ]

    async def wait_for_reply(self, body: str) -> None:
        async def said() -> bool:
            return body in self.replies()

        await wait_until(f"{body!r} in the room", said, check_alive=self._check_alive)

    def wait_for_typing(self, user_ids: list[str]) -> None:
        """Sync until the room's typing list is exactly *user_ids*.

        Blocking, and it has to be: typing is ephemeral and in no timeline, so the only way to see
        it is to be syncing while it happens. The position reached is kept, so a second call
        watches for the next change rather than re-reading the one that has already been seen.
        """
        deadline = time.monotonic() + _TYPING_BUDGET_SECONDS
        seen: list[list[str]] = []
        while time.monotonic() < deadline:
            batch = self._account.sync(since=self._typing_watermark, timeout_ms=1000)
            self._typing_watermark = batch["next_batch"]
            room = batch.get("rooms", {}).get("join", {}).get(self.room_id, {})
            for event in room.get("ephemeral", {}).get("events", []):
                if event["type"] != "m.typing":
                    continue
                seen.append(event["content"]["user_ids"])
                if seen[-1] == user_ids:
                    return
        raise WedgedError(f"typing never settled on {user_ids}; saw {seen}")
