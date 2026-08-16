"""The operator's side of a room: what a person sitting in it can do, and what they see.

Driven with `nio.AsyncClient` against the homeserver, so login, sends, edits, redactions,
`/messages` pagination and typing are the library's problem rather than hand-written HTTP.

**Independent of `matrix_client.py`, deliberately, and nothing here may import from it.** The
production client is nio plus *our* layer — `EventTag` and the transaction ids derived from it,
`SyncResult`, the mapping of nio's parse onto `InboundMessage`/`UnmappableEvent`, the pacer, the
outbox — and that layer is the thing under test. A test that read the room back through it would
agree with itself about any bug in it: a reply the console never sent and a reading that skipped
it look identical. Sharing nio underneath costs nothing, because nio is third party and is not
the subject; sharing a type or a tag parser would cost everything, which is why a
`works.allegedly.haku` assertion spells the key out in the test instead.

What this hands back is a `RoomEvent`, not nio's parse and not the wire's JSON. nio has no notion
of an edit — an `m.replace` arrives as an ordinary message whose body is `* the new text` — so the
three shapes the console actually produces become three variants here, and "is this a reply or the
status line being rewritten?" is an `isinstance` rather than a check for a key whose absence means
something.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from nio import (
    AsyncClient,
    BadEvent,
    Event,
    LoginResponse,
    MessageDirection,
    RedactedEvent,
    RedactionEvent,
    Response,
    RoomCreateResponse,
    RoomGetEventResponse,
    RoomMessageEmote,
    RoomMessageFormatted,
    RoomMessageImage,
    RoomMessageMedia,
    RoomMessageNotice,
    RoomMessagesResponse,
    RoomMessageText,
    RoomPreset,
    RoomSendResponse,
    SyncResponse,
    TypingNoticeEvent,
    UnknownBadEvent,
)

# `nio`'s package surface omits it; this is where it is defined.
from nio.api import RelationshipType

from haku.console.x.testing.synapse_container import HomeserverError
from haku.console.x.testing.waiting import WedgedError, wait_until

# How long an ephemeral event may take to reach the other side of the room.
_TYPING_BUDGET_SECONDS = 10.0

# One page is the whole room: these are conversations of tens of messages. The one test that
# overfills a room past a page reads it back through the client under test, not through here.
_TIMELINE_PAGE = 500


def _ok[R: Response](response: Response, expected: type[R]) -> R:
    """nio answers with a response *or* an error object; this is where an error stops being one."""
    if not isinstance(response, expected):
        raise HomeserverError(f"expected {expected.__name__}, got {response}")
    return response


class MessageKind(StrEnum):
    """The `msgtype` of a message these rooms carry.

    Strict: a msgtype outside this set is a console that started saying something new, which a
    test should hear about rather than silently classify as "not a reply".
    """

    TEXT = "m.text"
    NOTICE = "m.notice"
    EMOTE = "m.emote"
    IMAGE = "m.image"


# nio has a class per msgtype, so the msgtype is the class rather than a string to compare.
_KINDS = {
    RoomMessageText: MessageKind.TEXT,
    RoomMessageNotice: MessageKind.NOTICE,
    RoomMessageEmote: MessageKind.EMOTE,
    RoomMessageImage: MessageKind.IMAGE,
}


@dataclass(frozen=True)
class Message:
    """Something said in the room, whole.

    `formatted_body` is None where there is no HTML rendering to show — a message sent as plain
    text, or one whose msgtype carries no formatting at all.
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


def _parse(event: Event | BadEvent | UnknownBadEvent) -> RoomEvent:
    match event:
        case RedactedEvent():
            return Redacted(event_id=event.event_id, sender=event.sender, reason=event.reason)
        case RedactionEvent():
            return Redaction(event_id=event.event_id, sender=event.sender, redacts=event.redacts)
        case RoomMessageFormatted() | RoomMessageMedia():
            content = event.source["content"]
            if (new_content := content.get("m.new_content")) is not None:
                return Edit(
                    event_id=event.event_id,
                    sender=event.sender,
                    replaces=content["m.relates_to"]["event_id"],
                    body=event.body,
                    new_body=new_content["body"],
                    content=content,
                )
            return Message(
                event_id=event.event_id,
                sender=event.sender,
                kind=_KINDS[type(event)],
                body=event.body,
                formatted_body=event.formatted_body if isinstance(event, RoomMessageFormatted) else None,
            )
        case Event():
            return StateChange(event_id=event.event_id, sender=event.sender, event_type=event.source["type"])
        case _:
            raise HomeserverError(f"the homeserver sent something nio could not read: {event}")


def _parse_edit(event: Event | BadEvent | UnknownBadEvent) -> Edit:
    if not isinstance(parsed := _parse(event), Edit):
        raise ValueError(f"the homeserver indexed a non-edit as one: {event}")
    return parsed


@asynccontextmanager
async def sign_in(homeserver: str, user_id: str, password: str) -> AsyncIterator[AsyncClient]:
    """One logged-in Matrix user, closed with its connection pool."""
    client = AsyncClient(homeserver, user_id)
    try:
        _ok(await client.login(password), LoginResponse)
        yield client
    finally:
        await client.close()


class OperatorRoom:
    """One room, driven and read as the operator.

    *check_alive* is handed to every wait: a test whose console has died fails at the death rather
    than at the deadline. Omitted where nothing but the homeserver is serving the room.
    """

    def __init__(
        self, client: AsyncClient, *, bot_user_id: str, room_id: str, check_alive: Callable[[], None] | None = None
    ):
        self.room_id = room_id
        self._client = client
        self._bot = bot_user_id
        self._check_alive = check_alive

    @classmethod
    async def invite(
        cls, client: AsyncClient, *, bot_user_id: str, check_alive: Callable[[], None] | None = None
    ) -> OperatorRoom:
        """A fresh private room with Haku invited to it. Haku joins it itself, on its own `/sync` (R3.6)."""
        created = _ok(
            await client.room_create(preset=RoomPreset.private_chat, invite=[bot_user_id]), RoomCreateResponse
        )
        return cls(client, bot_user_id=bot_user_id, room_id=created.room_id, check_alive=check_alive)

    async def say(self, body: str) -> str:
        return await self.send({"msgtype": MessageKind.TEXT, "body": body})

    async def send(self, content: dict[str, Any]) -> str:
        """Put an arbitrary `m.room.message` in the room.

        Content rather than an argument per msgtype, because what this exists for is the msgtypes
        a person's client sends and this API does not model — an image, with the `url` that makes
        it one. What comes back is still read as a `RoomEvent`.
        """
        sent = await self._client.room_send(self.room_id, message_type="m.room.message", content=content)
        return _ok(sent, RoomSendResponse).event_id

    async def timeline(self) -> list[RoomEvent]:
        """Everything in the room, oldest first."""
        page = await self._client.room_messages(
            self.room_id, start="", direction=MessageDirection.back, limit=_TIMELINE_PAGE
        )
        return [_parse(event) for event in reversed(_ok(page, RoomMessagesResponse).chunk)]

    async def event(self, event_id: str) -> RoomEvent:
        return _parse(_ok(await self._client.room_get_event(self.room_id, event_id), RoomGetEventResponse).event)

    async def content_of(self, event_id: str) -> dict[str, Any]:
        """The event's `content` off the wire, for the keys this API deliberately does not model.

        The console's `works.allegedly.haku` tag rides in there, and a person sitting in the room
        does not see it — so it is not a field of `Message`, and a test that is about the tag
        surviving the homeserver reads it from here. The same half `Edit.content` carries.
        """
        fetched = _ok(await self._client.room_get_event(self.room_id, event_id), RoomGetEventResponse)
        content: dict[str, Any] = fetched.event.source["content"]
        return content

    async def edits_of(self, event_id: str) -> list[Edit]:
        """The homeserver's own index of what replaced *event_id*, rather than our reading of the timeline."""
        relations = self._client.room_get_event_relations(self.room_id, event_id, rel_type=RelationshipType.replacement)
        return [_parse_edit(event) async for event in relations]

    async def replies(self) -> list[str]:
        """What Haku said into the room, oldest first.

        `m.text` only: everything the console says *about* the conversation — joining, sandbox
        narration, the status line, lifecycle — is an `m.notice`, and a rewritten one is an `Edit`
        rather than a message of its own.
        """
        return [
            event.body
            for event in await self.timeline()
            if isinstance(event, Message) and event.sender == self._bot and event.kind is MessageKind.TEXT
        ]

    async def wait_for_reply(self, body: str) -> None:
        async def said() -> bool:
            return body in await self.replies()

        await wait_until(f"{body!r} in the room", said, check_alive=self._check_alive)

    async def wait_for_typing(self, user_ids: list[str]) -> None:
        """Sync until the room's typing list is exactly *user_ids*.

        Typing is ephemeral and in no timeline, so the only way to see it is to be syncing while
        it happens. The client keeps its own position between calls, so a second call watches for
        the next change rather than re-reading the one already seen.
        """
        deadline = time.monotonic() + _TYPING_BUDGET_SECONDS
        seen: list[list[str]] = []
        while time.monotonic() < deadline:
            batch = _ok(await self._client.sync(timeout=1000), SyncResponse)
            if (room := batch.rooms.join.get(self.room_id)) is None:
                continue
            for event in room.ephemeral:
                if not isinstance(event, TypingNoticeEvent):
                    continue
                seen.append(event.users)
                if seen[-1] == user_ids:
                    return
        raise WedgedError(f"typing never settled on {user_ids}; saw {seen}")
