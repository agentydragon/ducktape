"""The Matrix surface end to end, and the one question the operator actually asks of it:
**did every message I sent get an answer?**

Everything below this runs as itself. A real Synapse in a container, a console replica as its own
process (`testing/console_replica.py`) with the real `/sync` loop and neutral runtime supervisor, a real
runner process per sandbox with a stub `claude` behind it, and a real Postgres under all of it.
The operator's side is a Matrix client of its own (`testing/operator_room.py`, `nio` against the
same homeserver), so what a test reads back is the room, not the console's account of the room.

The property is the same in every test here: the bodies Haku posted, in order, are `re: ` and what
the operator typed. It fails on a message that was never answered and on one answered twice, which
are the two ways an outbound path can be wrong.

Three of these are the ways a produced reply could be lost without the console noticing — a
delivery that raised, a reply still on an in-process queue when the replica stopped, and a roll
across the gap between recording an answer and saying it. What closes all three is the durable room
outbox (`outbox.py`): the reply is a row written with the message it copies, and a drain says it
and marks it sent only once the homeserver has taken it.
`test_a_quiet_run_replies_once_to_every_message` is the control.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from secrets import token_hex
from uuid import UUID

import pytest
import pytest_bazel
from nio import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.x.channels.matrix.testing.console_deployment import Deployment
from haku.console.x.channels.matrix.testing.operator_room import OperatorRoom, sign_in
from haku.console.x.channels.matrix.testing.synapse_container import Synapse, run_synapse
from haku.console.x.session_store import SessionStore

PASSWORD = "not-a-secret"


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
async def deployment(
    synapse: Synapse,
    operator_user_id: str,
    migrated_db_url: str,
    migrated_sessions: async_sessionmaker[AsyncSession],
    session_store: SessionStore,
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> AsyncIterator[Deployment]:
    deployment = Deployment(
        name=request.node.name,
        homeserver=synapse.base_url,
        bot_user_id=synapse.create_user(f"haku{token_hex(6)}", PASSWORD),
        bot_password=PASSWORD,
        operator_user_id=operator_user_id,
        database_url=migrated_db_url,
        sessions=migrated_sessions,
        store=session_store,
        state=tmp_path,
    )
    try:
        yield deployment
    finally:
        await deployment.aclose()


@pytest.fixture
async def room(operator: AsyncClient, deployment: Deployment) -> OperatorRoom:
    return await OperatorRoom.invite(operator, bot_user_id=deployment.bot_user_id, check_alive=deployment.check_alive)


async def start_serving(deployment: Deployment, room: OperatorRoom) -> UUID:
    """Bind the room, then let its first prompt create the session and buy the sandbox.

    The room-joined notice is the pre-prompt sync barrier: the console has attached the durable
    conversation, but neutral supervision has deliberately created neither session nor claim yet.
    The operator's first message is durable demand; only after sending it can this helper wait for
    a runner to connect.
    """
    await deployment.start_console("console-1")
    await room.wait_for_notice("joined — this is now Haku's room")
    await room.say("one")
    session_id = await deployment.serving()
    await room.wait_for_reply("re: one")
    return session_id


async def test_a_quiet_run_replies_once_to_every_message(deployment: Deployment, room: OperatorRoom) -> None:
    """The control: same homeserver, same console process, same runner, same stub, so a failure in
    any of the others is the condition that test introduced rather than this harness. It also
    covers the other half of "answered exactly once" — a replayed frame or a re-sent answer would
    show up here as a duplicate.
    """
    await start_serving(deployment, room)
    for body in ("two", "three"):
        await room.say(body)
        await room.wait_for_reply(f"re: {body}")

    assert await room.replies() == ["re: one", "re: two", "re: three"]


async def test_a_second_room_is_served_as_its_own_conversation(
    deployment: Deployment, room: OperatorRoom, operator: AsyncClient
) -> None:
    """Attachment-scoped delivery, end to end: a second invite is joined and served beside the
    first, each room answered from its own conversation by its own session — and neither room ever
    shows the other's traffic, which is what per-attachment cursors, outboxes and budgets exist to
    guarantee.
    """
    await deployment.start_console("console-1")
    await room.wait_for_notice("joined — this is now Haku's room")
    second = await OperatorRoom.invite(operator, bot_user_id=deployment.bot_user_id, check_alive=deployment.check_alive)
    await second.wait_for_notice("joined — this is now Haku's room")

    await room.say("one")
    await second.say("uno")
    await room.wait_for_reply("re: one")
    await second.wait_for_reply("re: uno")
    await room.say("two")
    await room.wait_for_reply("re: two")

    assert await room.replies() == ["re: one", "re: two"]
    assert await second.replies() == ["re: uno"]


async def test_a_reply_whose_send_is_refused_is_said_on_a_later_attempt(
    deployment: Deployment, room: OperatorRoom
) -> None:
    """A refused send, with the console still running and nothing else going wrong.

    The refusal is the row's: unsent, one attempt spent, retried after its backoff. Two refusals
    are armed rather than one, so both kinds of reply are covered — `two` is an ordinary assistant
    message, and `three`, which the agent answers with a `result` frame and no assistant message at
    all, is the row `_run_turn` mints off that frame.

    **Order is part of the assertion.** A refused reply holds the queue rather than being
    overtaken, so `four` — produced while the earlier two were still waiting out their backoff —
    still arrives last.
    """
    session_id = await start_serving(deployment, room)

    deployment.refuse_the_next_reply()
    await room.say("two")
    await deployment.wait_until_refused()
    await deployment.wait_for_finished_turns(session_id, 2)

    deployment.refuse_the_next_reply()
    await room.say("three [silent]")
    await deployment.wait_until_refused()
    await deployment.wait_for_finished_turns(session_id, 3)

    # A message the room *can* answer, so what is asserted is a settled transcript rather than one
    # still in flight — and evidence that the console kept working after each dropped reply.
    await room.say("four")
    await room.wait_for_reply("re: four")

    assert await room.replies() == ["re: one", "re: two", "re: three", "re: four"]


async def test_a_reply_still_queued_when_the_console_stops_is_said_by_its_replacement(
    deployment: Deployment, room: OperatorRoom
) -> None:
    """A produced reply the console has not yet said, when the process goes away.

    The gap is opened by an armed homeserver refusal: the reply's first attempt is refused, so the
    row waits out its backoff — and the 25 narration lines collapse into the session line's edits
    instead of queueing ahead of it, which is why the refusal now carries the gap alone. A second
    refusal is armed for the retry, so the first console cannot say it before the stop.

    Both halves of the assertion matter: with the process *gone* the room still does not have the
    answer — a row that had somehow been sent would make the second half vacuous — and a
    replacement console says it without the agent, the runner or the operator doing anything.
    Nothing but the row carries the answer across.
    """
    session_id = await start_serving(deployment, room)

    deployment.refuse_the_next_reply()
    await room.say("two [narrate=25]")
    await deployment.wait_until_refused()
    deployment.refuse_the_next_reply()
    await deployment.wait_until_recorded(session_id, "re: two")
    assert "re: two" not in await room.replies(), "the answer reached the room before the gap could be opened"

    await deployment.stop()
    assert "re: two" not in await room.replies(), "a room no console is serving gained a message"

    await deployment.start_console("console-2")
    await room.wait_for_reply("re: two")

    assert await room.replies() == ["re: one", "re: two"]


async def test_every_message_is_answered_exactly_once_across_a_console_roll(
    deployment: Deployment, room: OperatorRoom
) -> None:
    """The headline: three messages, a console roll in the middle of the second, one reply each.

    The roll happens in the gap this module is about — the answer recorded, the room not yet told.
    The held `result` is what opens it: a message stays an open item until the wire says it ended,
    so its prose is durable while nothing can queue it for the room. The runner replays the frame,
    which `RolloutRecorder.received` refuses as one this session already has, so nothing but the
    record carries the answer across; the second console reads the completion and drains the row it
    writes.

    **Exactly once, so this fails on a duplicate as well as on a drop.** Re-deriving the same reply
    collides with the outbox's unique subject rather than adding a second copy.

    The agent holds its `result` across the roll on purpose: that leaves the turn open, so the
    second console adopts an exchange in flight rather than finding a finished one — and the 25
    narration lines fold into the session line's edits rather than delaying anything.
    """
    session_id = await start_serving(deployment, room)

    await room.say("two [narrate=25] [hold]")
    await deployment.wait_until_holding()
    await deployment.wait_until_recorded(session_id, "re: two")
    assert "re: two" not in await room.replies(), "the answer reached the room before the roll could be timed"

    await deployment.stop()
    await deployment.start_console("console-2")
    deployment.release_the_agent()
    await deployment.wait_for_finished_turns(session_id, 2)

    await room.say("three")
    await room.wait_for_reply("re: three")

    assert await room.replies() == ["re: one", "re: two", "re: three"]


async def test_a_message_sent_mid_turn_is_rejected_in_the_room_rather_than_answered_late(
    deployment: Deployment, room: OperatorRoom
) -> None:
    """Nothing queues behind a running turn, end to end.

    The agent holds its answer to `two`, so `three` arrives while that turn is open. It is not
    delivered and not held — the room is told so while the turn is still running, and the operator
    sends it again once Haku is free. What proves it was a rejection rather than a delay is the
    reply list: `re: three` is there once, and only after the re-send.
    """
    session_id = await start_serving(deployment, room)

    await room.say("two [hold]")
    await deployment.wait_until_holding()
    await room.say("three")
    await room.wait_for_notice("not delivered")

    deployment.release_the_agent()
    await deployment.wait_for_finished_turns(session_id, 2)
    await room.say("three")
    await room.wait_for_reply("re: three")

    assert await room.replies() == ["re: one", "re: two", "re: three"]


async def test_a_message_accepted_by_a_dying_session_is_answered_after_a_restart(
    deployment: Deployment, room: OperatorRoom
) -> None:
    """Seen, acknowledged, and never answered — the case a bare "skip what we have seen" loses.

    A killed sandbox leaves the session `ready` for a whole `ADOPTION_GRACE`, so `two` is
    **accepted** by a session that will never claim it, and accepting it acknowledged the batch to
    the homeserver: nothing re-delivers it and the prompt row is the only copy left. The console is
    then stopped, so what finds that row is a process that has just come up and knows only what the
    record says — the recovery reads what is still owed rather than trusting the position.
    """
    doomed = await start_serving(deployment, room)

    await deployment.kill_sandbox(doomed)
    await room.say("two")
    await deployment.wait_until_queued(doomed, "two")
    await deployment.stop()

    await deployment.start_console("console-2")
    await deployment.serving(after=doomed)
    await room.wait_for_reply("re: two")

    assert await room.replies() == ["re: one", "re: two"]


async def test_a_batch_the_homeserver_re_delivers_is_answered_once(deployment: Deployment, room: OperatorRoom) -> None:
    """The crash window the ledger closes, opened by hand because nothing else can open it.

    The prompt commits and the watermark commits after it, so a console that dies in between
    acknowledges nothing and `/sync` hands the same events back. Rewinding the position with the
    console stopped is exactly that state, and what must not follow is a second answer: the events
    are recognised as ones a prompt in the record already carries and dropped from the batch.

    `three` is what makes the assertion a settled transcript rather than one still in flight, and
    evidence that the console kept reading the room after the replay.
    """
    await start_serving(deployment, room)
    acknowledged = await deployment.sync_position()

    await room.say("two")
    await room.wait_for_reply("re: two")
    await deployment.stop()
    await deployment.rewind_sync_to(acknowledged)

    await deployment.start_console("console-2")
    await room.say("three")
    await room.wait_for_reply("re: three")

    assert await room.replies() == ["re: one", "re: two", "re: three"]


async def test_a_replacement_session_wakes_from_our_transcript_rather_than_from_the_room(
    deployment: Deployment, room: OperatorRoom
) -> None:
    """**Which copy of the conversation is this?**

    `two` is what tells the copies apart, and it is the load-bearing case anyway. The killed
    sandbox is how a replacement session gets made at all; `two` is accepted by the dying session
    and never answered by it, so it sits past the sync watermark and only our own record can carry
    it into the replacement — re-offered as the replacement's first prompt, while the rendered
    history deliberately stops before it (`ConversationHistory.recent` excludes the wakened
    session's own queued prompt) so the replacement is not told the same message twice.

    Form no longer distinguishes them: the event ids ingress once wrote inline ride on the prompt's
    own event now, so both copies carry the same text and only position separates them.
    """
    doomed = await start_serving(deployment, room)

    await deployment.kill_sandbox(doomed)
    await room.say("two")
    await deployment.wait_until_queued(doomed, "two")
    await deployment.serving(after=doomed)
    # The replacement launches its CLI on its first prompt, and the launch is what renders the
    # system prompt this test reads. That prompt is `two` itself, offered again because the session
    # that accepted it died holding it — so waiting for its answer is waiting for the launch.
    await room.wait_for_reply("re: two")
    await room.say("three")
    await room.wait_for_reply("re: three")

    launched = deployment.system_prompts()
    assert len(launched) >= 2, "no replacement session was ever started"
    woken = launched[-1]
    assert "re: one" in woken, "half a conversation is not context — Haku's own reply is there too"
    # `re: two` above proves the accepted-and-unanswered message reached the replacement — as its
    # re-offered first prompt. The rendered history therefore must NOT repeat it: one copy, as the
    # prompt. (The old wording "the two of you were saying" used to satisfy a `"two" in woken`
    # here whatever the history held.)
    assert "two" not in woken, "the re-offered prompt must not also be rendered as history"


if __name__ == "__main__":
    pytest_bazel.main()
