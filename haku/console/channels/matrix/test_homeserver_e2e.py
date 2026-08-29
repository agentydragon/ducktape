"""`Client` against a real Synapse, rather than against canned responses.

Every one of these is a property of Synapse that the Matrix surface is built on and that a fake
would agree with whatever the code did:

- a `/sync` resumed across a gap larger than `TIMELINE_LIMIT` really does come back truncated, and
  the pagination that closes it really does land on every missed message, once, in order (downtime
  recovery — the property this module exists for);
- a `/sync` watermark really is accepted as a `/messages` pagination token, at both ends;
- a repeated transaction id really is refused as a second event
  (<../../docs/conversation_runtime_facts.md>);
- `works.allegedly.haku` really does survive a round trip through the homeserver, including the
  copy that rides inside `m.new_content` — and the correspondence reader really does get our own
  sends, edits and redactions back off the same `/sync`.

The room's other side is driven through the operator-side API (`testing/operator_room.py`), a
second nio client that shares no code with `Client` — a test that checks the client against
itself checks nothing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from secrets import token_hex
from uuid import uuid4

import pytest
import pytest_bazel
from nio import AsyncClient, DevicesResponse

from haku.console.channels.matrix.client import (
    HAKU_CONTENT_KEY,
    TIMELINE_LIMIT,
    Client,
    ConversationEventSource,
    EventTag,
    Invite,
    RoomEventKind,
)
from haku.console.channels.matrix.outbox import PendingReply
from haku.console.channels.matrix.testing.operator_room import Message, MessageKind, OperatorRoom, Redacted, sign_in
from haku.console.channels.matrix.testing.synapse_container import Synapse, run_synapse

PASSWORD = "not-a-secret"

# What the console pins in production, and the point of pinning it: every login lands on one
# device, which is what Synapse keys its transaction cache on.
DEVICE_ID = "haku-console"


@dataclass(frozen=True)
class Bot:
    """Haku's side of the room: the client under test, logged in."""

    client: Client
    user_id: str
    token: str


@pytest.fixture(scope="session")
def synapse() -> Iterator[Synapse]:
    with run_synapse() as homeserver:
        yield homeserver


@pytest.fixture
def operator_user_id(synapse: Synapse) -> str:
    """A fresh operator per test — the homeserver is shared, so nothing else may be."""
    return synapse.create_user(f"operator{token_hex(6)}", PASSWORD)


@pytest.fixture
async def operator(synapse: Synapse, operator_user_id: str) -> AsyncIterator[AsyncClient]:
    async with sign_in(synapse.base_url, operator_user_id, PASSWORD) as client:
        yield client


@pytest.fixture
async def bot(synapse: Synapse) -> AsyncIterator[Bot]:
    user_id = synapse.create_user(f"haku{token_hex(6)}", PASSWORD)
    client = Client(synapse.base_url, user_id, DEVICE_ID)
    try:
        yield Bot(client, user_id, await client.login(PASSWORD))
    finally:
        await client.close()


@pytest.fixture
async def joined_room(bot: Bot, operator: AsyncClient) -> OperatorRoom:
    """A room the operator invited Haku into and Haku is in."""
    room = await OperatorRoom.invite(operator, bot_user_id=bot.user_id)
    await bot.client.join(bot.token, room.room_id)
    return room


async def test_login_lands_on_the_pinned_device_and_whoami_rejects_a_stale_token(bot: Bot, synapse: Synapse) -> None:
    """The console caches its token and re-logs in only when it stops working.

    `whoami` is what decides between the two, so it has to answer no for a token the homeserver has
    never heard of rather than raising, and yes for one it issued. The device is pinned because
    Synapse keys its transaction dedup on the device rather than on the token: a login per restart
    would quietly cost the guarantee `PendingReply.transaction_id` rests on.
    """
    again = await bot.client.login(PASSWORD)

    assert again != bot.token, "a second login issues a second token"
    assert await bot.client.whoami(again)
    assert not await bot.client.whoami("not-a-token-this-homeserver-ever-issued")

    # Read with the token the second login issued, so the count is of what the two logins left
    # behind and not of the reader as well.
    reader = AsyncClient(synapse.base_url, bot.user_id)
    reader.access_token = again
    try:
        devices = await reader.devices()
        assert isinstance(devices, DevicesResponse)
        assert [device.id for device in devices.devices] == [DEVICE_ID]
    finally:
        await reader.close()


async def test_an_invite_from_the_operator_becomes_a_serviced_room(
    bot: Bot, operator: AsyncClient, operator_user_id: str
) -> None:
    """The operator puts Haku in the room, and only then is Haku in it."""
    room = await OperatorRoom.invite(operator, bot_user_id=bot.user_id)

    invited = await bot.client.sync(bot.token, since=None)
    assert invited.invites == (Invite(room_id=room.room_id, inviter=operator_user_id),)
    assert invited.messages == (), "an invite is not a message, and a first sync replays no backlog"

    await bot.client.join(bot.token, room.room_id)
    await room.say("hello")

    assert [message.body for message in (await bot.client.sync(bot.token, since=invited.next_batch)).messages] == [
        "hello"
    ]


async def test_a_gap_larger_than_the_timeline_limit_delivers_every_message_once(
    bot: Bot, joined_room: OperatorRoom, caplog: pytest.LogCaptureFixture
) -> None:
    """No message is lost across console downtime, however long the gap.

    Past `TIMELINE_LIMIT` events a resumed `/sync` answers with a **truncated** view and flags it
    rather than erroring, so a reader that does not check the flag skips the difference silently
    and nothing about the response looks wrong.

    So: stop syncing, overfill the room, resume from the stored watermark, and require the whole
    gap back in order. The backfill warning is asserted too — without it a change that stopped
    truncating (a larger limit, a smaller batch) would leave this passing while testing nothing.
    """
    watermark = (await bot.client.sync(bot.token, since=None)).next_batch
    missed = [f"message {index:03d}" for index in range(TIMELINE_LIMIT + 50)]
    for body in missed:
        await joined_room.say(body)

    with caplog.at_level("WARNING"):
        recovered = await bot.client.sync(bot.token, since=watermark)

    assert "timeline truncated, backfilling" in caplog.text, "the gap was not truncated — this proved nothing"
    assert [message.body for message in recovered.messages] == missed

    # Exactly once: what the watermark has covered is not offered again.
    await joined_room.say("after the gap")
    resumed = await bot.client.sync(bot.token, since=recovered.next_batch)
    assert [message.body for message in resumed.messages] == ["after the gap"]


async def test_an_unreadable_event_is_reported_and_the_report_cannot_become_one(
    bot: Bot, operator_user_id: str, joined_room: OperatorRoom
) -> None:
    """The loop an unreadable-event notice would create if the guard were wrong.

    The notice Haku posts about an event it cannot read is itself an event in the room and comes
    back on the very next `/sync`. If a notice were reportable, one screenshot would produce a
    notice, which would produce a notice, until the room's send budget was the only thing left
    stopping it — so the round trip through the homeserver is the test, not the classification in
    isolation.

    Also the msgtype split: an emote is prose the operator typed and is serviced, an image is not
    and is reported, and both arrive in the same batch as the sentence next to them.
    """
    watermark = (await bot.client.sync(bot.token, since=None)).next_batch
    image = await joined_room.send({"msgtype": "m.image", "body": "screenshot.png", "url": "mxc://test/none"})
    await joined_room.send({"msgtype": "m.emote", "body": "waves"})
    await joined_room.say("look at this")

    seen = await bot.client.sync(bot.token, since=watermark)

    assert [message.body for message in seen.messages] == ["waves", "look at this"]
    assert [(event.event_id, event.msgtype, event.sender) for event in seen.unmappable] == [
        (image, "m.image", operator_user_id)
    ]

    tag = EventTag(kind=RoomEventKind.UNREADABLE)
    await bot.client.send_notice(
        bot.token, joined_room.room_id, "received 1 message(s) Haku cannot read", txn_id=tag.transaction_id(), tag=tag
    )
    echoed = await bot.client.sync(bot.token, since=seen.next_batch)

    assert (echoed.messages, echoed.unmappable) == ((), ())


async def test_a_tag_and_its_rendering_survive_the_homeserver(bot: Bot, joined_room: OperatorRoom) -> None:
    """The `works.allegedly.haku` key rides in `content`, so the homeserver is what it survives."""
    tag = EventTag(kind=RoomEventKind.REPLY, conversation_id=uuid4())

    event_id = await bot.client.send_text(
        bot.token, joined_room.room_id, "**bold** answer", txn_id=tag.transaction_id(), tag=tag
    )

    assert await joined_room.event(event_id) == Message(
        event_id=event_id,
        sender=bot.user_id,
        kind=MessageKind.TEXT,
        body="**bold** answer",  # the Markdown source stays the fallback
        formatted_body="<p><strong>bold</strong> answer</p>",
    )
    # Off the raw content, because a person in the room does not see a tag (`EventTag`); what the
    # console's own reader makes of one is the correspondence test below.
    assert (await joined_room.content_of(event_id))[HAKU_CONTENT_KEY] == tag.content()


async def test_the_own_copy_reader_sees_sends_edits_and_redactions(bot: Bot, joined_room: OperatorRoom) -> None:
    """The correspondence reader against a real homeserver.

    Our tagged events come back through the same `/sync` ingress uses, with the source the writer
    put on them readable; an edit surfaces naming the event it replaces; and a redaction — the
    operator unsaying Haku's copy — is surfaced too. None of it is ever input.
    """
    watermark = (await bot.client.sync(bot.token, since=None)).next_batch
    tag = EventTag(
        kind=RoomEventKind.LIFECYCLE,
        source=ConversationEventSource(attachment_id=uuid4(), conversation_id=uuid4(), event_seq=7),
    )
    event_id = await bot.client.send_notice(
        bot.token, joined_room.room_id, "session ended", txn_id=tag.transaction_id(), tag=tag
    )

    seen = await bot.client.sync(bot.token, since=watermark)
    [projected] = seen.projected
    assert (projected.event_id, projected.source, projected.replaces_event_id) == (event_id, tag.source, None)
    assert (seen.messages, seen.unmappable) == ((), ())

    # A fresh transaction id: reusing the source-derived one would be refused as the send above.
    await bot.client.edit_notice(
        bot.token, joined_room.room_id, event_id, "session ended, edited", txn_id=uuid4().hex, tag=tag
    )
    edited = await bot.client.sync(bot.token, since=seen.next_batch)
    [edit] = edited.projected
    assert (edit.replaces_event_id, edit.source) == (event_id, tag.source)

    await joined_room.redact(event_id, reason="unsaid by the operator")
    redacted = await bot.client.sync(bot.token, since=edited.next_batch)
    assert [redaction.redacts_event_id for redaction in redacted.redactions] == [event_id]


async def test_the_same_outbox_row_cannot_post_twice(bot: Bot, joined_room: OperatorRoom) -> None:
    """<../../docs/conversation_runtime_facts.md> — Synapse deduplicates a transaction per device.

    A reply is sent under its outbox row's id (`PendingReply.transaction_id`), so a replacement
    replica redriving the same row is refused rather than posting it twice. The device is what the
    cache is keyed on, and `Client` pins one.
    """
    reply = PendingReply(
        outbox_id=uuid4(),
        attachment_id=uuid4(),
        room_id=joined_room.room_id,
        subject=uuid4().hex,
        body="the answer",
        attempts=0,
    )

    room = joined_room.room_id
    first = await bot.client.send_text(bot.token, room, reply.body, txn_id=reply.transaction_id(), tag=reply.tag())
    again = await bot.client.send_text(bot.token, room, reply.body, txn_id=reply.transaction_id(), tag=reply.tag())

    assert first == again
    assert [event.event_id for event in await joined_room.timeline() if isinstance(event, Message)] == [first]


async def test_the_same_projected_notice_cannot_post_twice(bot: Bot, joined_room: OperatorRoom) -> None:
    """A cursor replay inside Synapse's transaction-cache window reaches the first notice."""
    tag = EventTag(
        kind=RoomEventKind.LIFECYCLE,
        source=ConversationEventSource(attachment_id=uuid4(), conversation_id=uuid4(), event_seq=7),
    )
    room = joined_room.room_id

    first = await bot.client.send_notice(bot.token, room, "session ended", txn_id=tag.transaction_id(), tag=tag)
    again = await bot.client.send_notice(bot.token, room, "session ended", txn_id=tag.transaction_id(), tag=tag)

    assert first == again
    assert [event.event_id for event in await joined_room.timeline() if isinstance(event, Message)] == [first]


async def test_an_edit_replaces_the_status_line_rather_than_adding_one(bot: Bot, joined_room: OperatorRoom) -> None:
    """One status line per turn, which is an `m.replace` and not a second notice."""
    room = joined_room.room_id
    tag = EventTag(kind=RoomEventKind.STATUS, conversation_id=uuid4())
    event_id = await bot.client.send_notice(bot.token, room, "thinking", txn_id=tag.transaction_id(), tag=tag)

    await bot.client.edit_notice(bot.token, room, event_id, "running Bash", txn_id=tag.transaction_id(), tag=tag)

    # The homeserver's own index of what replaces what, rather than our reading of the timeline.
    [edit] = await joined_room.edits_of(event_id)
    assert (edit.replaces, edit.new_body) == (event_id, "running Bash")
    assert edit.body == "* running Bash", "the fallback body is what a client that ignores edits shows"
    assert edit.content["m.new_content"][HAKU_CONTENT_KEY] == tag.content(), (
        "the tag rides on the half a client re-renders"
    )
    assert edit.content[HAKU_CONTENT_KEY] == tag.content()


async def test_a_redacted_event_is_gone_from_the_room(bot: Bot, joined_room: OperatorRoom) -> None:
    """A retired status line leaves nothing behind, and leaves it nowhere the room reads.

    A redaction is the one thing the room knows and our record does not — harmless for what
    redaction is used for here, since a status line was never recorded in the first place.
    """
    room = joined_room.room_id
    tag = EventTag(kind=RoomEventKind.REPLY)
    event_id = await bot.client.send_text(bot.token, room, "spent", txn_id=tag.transaction_id(), tag=tag)

    await bot.client.redact(bot.token, room, event_id, reason="turn finished")

    assert await joined_room.event(event_id) == Redacted(event_id=event_id, sender=bot.user_id, reason="turn finished")


async def test_the_typing_notice_starts_and_stops(bot: Bot, joined_room: OperatorRoom) -> None:
    """The room shows Haku thinking, and stops showing it when Haku stops."""
    await bot.client.set_typing(bot.token, joined_room.room_id, active=True)
    await joined_room.wait_for_typing([bot.user_id])

    await bot.client.set_typing(bot.token, joined_room.room_id, active=False)
    await joined_room.wait_for_typing([])


if __name__ == "__main__":
    pytest_bazel.main()
