"""`MatrixClient` against a real Synapse, rather than against canned responses.

What a hand-written homeserver cannot answer is what is here. Every one of these is a property
of Synapse that the Matrix surface is built on and that a fake would agree with whatever the
code did:

- a `/sync` resumed across a gap larger than `TIMELINE_LIMIT` really does come back truncated,
  and the pagination that closes it really does land on every missed message, once, in order
  (R1.7 — the requirement this whole module exists for);
- a `/sync` watermark really is accepted as a `/messages` pagination token, at both ends;
- a repeated transaction id really is refused as a second event
  (<../docs/chat_runtime_facts.md>);
- `works.allegedly.haku` really does survive a round trip through the homeserver, including
  the copy that rides inside `m.new_content`.

The room's other side is driven through the client-server API directly (`Account`), never
through `MatrixClient` — a test that checks the client against itself checks nothing.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from secrets import token_hex
from uuid import uuid4

import pytest
import pytest_bazel

from haku.console.x.matrix_client import TIMELINE_LIMIT, EventTag, Invite, MatrixClient, RoomEventKind
from haku.console.x.synapse_container import Account, Synapse, run_synapse

PASSWORD = "not-a-secret"

# What the console pins in production, and the point of pinning it: every login lands on one
# device, which is what Synapse keys its transaction cache on.
DEVICE_ID = "haku-console"

# How long an ephemeral event may take to reach the other side of the room.
_TYPING_BUDGET = 10.0


@dataclass(frozen=True)
class Bot:
    """Haku's side of the room: the client under test, logged in."""

    client: MatrixClient
    user_id: str
    token: str


@pytest.fixture(scope="session")
def synapse() -> Iterator[Synapse]:
    with run_synapse() as homeserver:
        yield homeserver


@pytest.fixture
def operator(synapse: Synapse) -> Iterator[Account]:
    """A fresh operator per test — the homeserver is shared, so nothing else may be."""
    account = synapse.sign_in(synapse.create_user(f"operator{token_hex(6)}", PASSWORD), PASSWORD)
    try:
        yield account
    finally:
        account.close()


@pytest.fixture
async def bot(synapse: Synapse) -> AsyncIterator[Bot]:
    user_id = synapse.create_user(f"haku{token_hex(6)}", PASSWORD)
    client = MatrixClient(synapse.base_url, user_id, DEVICE_ID)
    try:
        yield Bot(client, user_id, await client.login(PASSWORD))
    finally:
        await client.close()


@pytest.fixture
async def joined_room(bot: Bot, operator: Account) -> str:
    """A room the operator invited Haku into and Haku is in (R3.6)."""
    room_id = operator.create_room(invite=bot.user_id)
    await bot.client.join(bot.token, room_id)
    return room_id


def _settled_typing(operator: Account, room_id: str, since: str, expected: list[str]) -> str:
    """Sync until the room's typing list is *expected*, returning the position reached.

    Typing is ephemeral: it arrives on a `/sync` and is in no timeline, so the only way to see it
    is to be syncing when it happens.
    """
    deadline = time.monotonic() + _TYPING_BUDGET
    seen: list[list[str]] = []
    while time.monotonic() < deadline:
        batch = operator.sync(since=since, timeout_ms=1000)
        since = batch["next_batch"]
        room = batch.get("rooms", {}).get("join", {}).get(room_id, {})
        for event in room.get("ephemeral", {}).get("events", []):
            if event["type"] != "m.typing":
                continue
            seen.append(event["content"]["user_ids"])
            if seen[-1] == expected:
                return since
    raise AssertionError(f"typing never settled on {expected}; saw {seen}")


async def test_login_lands_on_the_pinned_device_and_whoami_rejects_a_stale_token(bot: Bot, synapse: Synapse) -> None:
    """R10.3a — the console caches its token and re-logs in only when it stops working.

    `whoami` is what decides between the two, so it has to answer no for a token the homeserver
    has never heard of rather than raising, and yes for one it issued. The device is pinned
    because Synapse keys its transaction dedup on the device rather than on the token: a login
    per restart would quietly cost the guarantee `EventTag.transaction_id` rests on.
    """
    again = await bot.client.login(PASSWORD)

    assert again != bot.token, "a second login issues a second token"
    assert await bot.client.whoami(again)
    assert not await bot.client.whoami("not-a-token-this-homeserver-ever-issued")

    # Read with the token the second login issued, so the count is of what the two logins left
    # behind and not of the reader as well.
    reader = Account(synapse.base_url, bot.user_id, again)
    try:
        assert [device["device_id"] for device in reader.devices()] == [DEVICE_ID]
    finally:
        reader.close()


async def test_an_invite_from_the_operator_becomes_a_serviced_room(bot: Bot, operator: Account) -> None:
    """R3.6 — the operator puts Haku in the room, and only then is Haku in it."""
    room_id = operator.create_room(invite=bot.user_id)

    invited = await bot.client.sync(bot.token, since=None)
    assert invited.invites == (Invite(room_id=room_id, inviter=operator.user_id),)
    assert invited.messages == (), "an invite is not a message, and a first sync replays no backlog (R1.7a)"

    await bot.client.join(bot.token, room_id)
    operator.send_text(room_id, "hello")

    assert [message.body for message in (await bot.client.sync(bot.token, since=invited.next_batch)).messages] == [
        "hello"
    ]


async def test_a_gap_larger_than_the_timeline_limit_delivers_every_message_once(
    bot: Bot, operator: Account, joined_room: str, caplog: pytest.LogCaptureFixture
) -> None:
    """R1.7 — no message is lost across console downtime, however long the gap.

    This is the one property a fake homeserver cannot be trusted about, and the one the
    requirement's gotcha is about: past `TIMELINE_LIMIT` events, a resumed `/sync` answers with a
    **truncated** view and flags it rather than erroring, so a reader that does not check the flag
    skips the difference silently. Nothing about the response looks wrong.

    So: stop syncing, overfill the room, resume from the stored watermark, and require the whole
    gap back in order. The backfill warning is asserted too — without it a change that stopped
    truncating (a larger limit, a smaller batch) would leave this passing while testing nothing.
    """
    watermark = (await bot.client.sync(bot.token, since=None)).next_batch
    missed = [f"message {index:03d}" for index in range(TIMELINE_LIMIT + 50)]
    for body in missed:
        operator.send_text(joined_room, body)

    with caplog.at_level("WARNING"):
        recovered = await bot.client.sync(bot.token, since=watermark)

    assert "timeline truncated, backfilling" in caplog.text, "the gap was not truncated — this proved nothing"
    assert [message.body for message in recovered.messages] == missed

    # Exactly once: what the watermark has covered is not offered again.
    operator.send_text(joined_room, "after the gap")
    resumed = await bot.client.sync(bot.token, since=recovered.next_batch)
    assert [message.body for message in resumed.messages] == ["after the gap"]


async def test_an_unreadable_event_is_reported_and_the_report_cannot_become_one(
    bot: Bot, operator: Account, joined_room: str
) -> None:
    """R1.6 against the real thing, and the loop it would create if the guard were wrong.

    The notice Haku posts about an event it cannot read is itself an event in the room, and comes
    back on the very next `/sync`. If a notice were reportable, one screenshot would produce a
    notice, which would produce a notice, until the room's send budget was the only thing left
    stopping it — so the round trip through the homeserver is the test, not the classification in
    isolation.

    Also the msgtype split: an emote is prose the operator typed and is serviced, an image is not
    and is reported, and both arrive in the same batch as the sentence next to them.
    """
    watermark = (await bot.client.sync(bot.token, since=None)).next_batch
    image = operator.send(joined_room, {"msgtype": "m.image", "body": "screenshot.png", "url": "mxc://test/none"})
    operator.send(joined_room, {"msgtype": "m.emote", "body": "waves"})
    operator.send_text(joined_room, "look at this")

    seen = await bot.client.sync(bot.token, since=watermark)

    assert [message.body for message in seen.messages] == ["waves", "look at this"]
    assert [(event.event_id, event.msgtype, event.sender) for event in seen.unmappable] == [
        (image, "m.image", operator.user_id)
    ]

    tag = EventTag(kind=RoomEventKind.UNREADABLE)
    await bot.client.send_notice(
        bot.token, joined_room, "received 1 message(s) Haku cannot read", txn_id=tag.transaction_id(), tag=tag
    )
    echoed = await bot.client.sync(bot.token, since=seen.next_batch)

    assert (echoed.messages, echoed.unmappable) == ((), ())


async def test_a_tag_and_its_rendering_survive_the_homeserver(bot: Bot, operator: Account, joined_room: str) -> None:
    """The `works.allegedly.haku` key rides in `content`, so the homeserver is what it survives."""
    tag = EventTag(kind=RoomEventKind.REPLY, session_id=uuid4(), message_id=uuid4(), agent_message_id="msg_01abc")

    event_id = await bot.client.send_text(
        bot.token, joined_room, "**bold** answer", txn_id=tag.transaction_id(), tag=tag
    )

    content = operator.event(joined_room, event_id)["content"]
    assert content["formatted_body"] == "<p><strong>bold</strong> answer</p>"
    assert content["body"] == "**bold** answer", "the Markdown source stays the fallback (R11.7)"

    # Read back the way a re-awakened session reads its history — which also says a `/sync`
    # watermark is a `/messages` pagination token.
    watermark = (await bot.client.sync(bot.token, since=None)).next_batch
    [remembered] = await bot.client.recent_messages(bot.token, joined_room, since=watermark, limit=5)
    assert (remembered.event_id, remembered.body, remembered.tag) == (event_id, "**bold** answer", tag)


async def test_the_same_transcript_row_cannot_post_twice(bot: Bot, operator: Account, joined_room: str) -> None:
    """<../docs/chat_runtime_facts.md> — Synapse deduplicates a transaction per device.

    `EventTag.transaction_id` derives a reply's transaction from the transcript row it is, so a
    replacement replica re-sending the same answer is refused rather than posting it twice. The
    device is what the cache is keyed on, and `MatrixClient` pins one.
    """
    tag = EventTag(kind=RoomEventKind.REPLY, message_id=uuid4())

    first = await bot.client.send_text(bot.token, joined_room, "the answer", txn_id=tag.transaction_id(), tag=tag)
    again = await bot.client.send_text(bot.token, joined_room, "the answer", txn_id=tag.transaction_id(), tag=tag)

    assert first == again
    assert [event["event_id"] for event in operator.messages(joined_room) if event["type"] == "m.room.message"] == [
        first
    ]


async def test_an_edit_replaces_the_status_line_rather_than_adding_one(
    bot: Bot, operator: Account, joined_room: str
) -> None:
    """R6.5 — one status line per turn, which is an `m.replace` and not a second notice."""
    tag = EventTag(kind=RoomEventKind.STATUS, session_id=uuid4())
    event_id = await bot.client.send_notice(bot.token, joined_room, "thinking", txn_id=tag.transaction_id(), tag=tag)

    await bot.client.edit_notice(bot.token, joined_room, event_id, "running Bash", txn_id=tag.transaction_id(), tag=tag)

    # The homeserver's own index of what replaces what, rather than our reading of the timeline.
    [edit] = operator.relations(joined_room, event_id, "m.replace")
    assert edit["content"]["m.new_content"]["body"] == "running Bash"
    assert edit["content"]["body"] == "* running Bash", "the fallback body is what a client that ignores edits shows"
    assert EventTag.parse(edit["content"]["m.new_content"]) == tag, "the tag rides on the half a client re-renders"
    assert EventTag.parse(edit["content"]) == tag


async def test_a_redacted_event_is_gone_from_the_room(bot: Bot, operator: Account, joined_room: str) -> None:
    """R6.5 — a retired status line leaves nothing behind, including for a re-awakened session."""
    tag = EventTag(kind=RoomEventKind.REPLY, message_id=uuid4())
    event_id = await bot.client.send_text(bot.token, joined_room, "spent", txn_id=tag.transaction_id(), tag=tag)

    await bot.client.redact(bot.token, joined_room, event_id, reason="turn finished")

    redacted = operator.event(joined_room, event_id)
    assert redacted["content"] == {}
    assert redacted["unsigned"]["redacted_because"]["content"]["reason"] == "turn finished"
    watermark = (await bot.client.sync(bot.token, since=None)).next_batch
    assert await bot.client.recent_messages(bot.token, joined_room, since=watermark, limit=5) == ()


async def test_the_typing_notice_starts_and_stops(bot: Bot, operator: Account, joined_room: str) -> None:
    """R6.1 — the room shows Haku thinking, and stops showing it when Haku stops."""
    since = operator.sync()["next_batch"]

    await bot.client.set_typing(bot.token, joined_room, active=True)
    since = _settled_typing(operator, joined_room, since, [bot.user_id])

    await bot.client.set_typing(bot.token, joined_room, active=False)
    _settled_typing(operator, joined_room, since, [])


if __name__ == "__main__":
    pytest_bazel.main()
