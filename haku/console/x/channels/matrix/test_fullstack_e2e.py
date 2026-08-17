"""The Matrix surface end to end, and the one question the operator actually asks of it:
**did every message I sent get an answer?**

Everything below this runs as itself. A real Synapse in a container, a console replica as its own
process (`testing/console_replica.py`) with the real `/sync` loop and session supervisor, a real
runner process per sandbox with a stub `claude` behind it, and a real Postgres under all of it.
The operator's side is a Matrix client of its own (`testing/operator_room.py`, `nio` against the
same homeserver), so what a test reads back is the room, not the console's account of the room.

The property is one line and it is the same in every test here: the bodies Haku posted, in order,
are `re: ` and what the operator typed. It fails on a message that was never answered and on one
answered twice, which are the two ways an outbound path can be wrong.

**Three ways a produced reply used to be lost**, none of which the console noticed. Each is a
test here, and each failed when this file was written against a console that delivered by handing
a closure to an in-process queue:

1. A delivery that raised was logged and dropped (`_deliver_reply`), and `spoke` was set anyway —
   so the end-of-turn fallback, the one thing that would have said the same text a second time,
   read "already said" and stayed quiet. One failed send made the whole turn silent. No reconnect
   and no roll involved; the plain case.
2. `_deliver_reply` only **queued**. `pacer` is an in-process deque draining at
   `SENDS_PER_SECOND`, so on a room with anything queued ahead of it "delivered" was minutes away
   from "spoken", and a console that stopped in between took the queue with it.
3. Across a roll neither was recovered. The frame is in `session_frames`, so the runner's replay
   is refused as one this session already has (`RolloutRecorder.received`), and `adopt_open_turn`
   read the same row as `spoke=True` — which its own docstring admits it cannot tell from
   delivered. Permanently recorded, permanently unspoken.

R11.6 says the opposite: "a produced reply must never be lost silently". What closes all three is
the durable room outbox (`outbox.py`): the reply is a row written with the message it
copies, and a drain says it and marks it sent only once the homeserver has taken it.
`test_a_quiet_run_replies_once_to_every_message` is the control, and it passed throughout.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from secrets import token_hex

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
    chat_store: SessionStore,
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
        store=chat_store,
        state=tmp_path,
    )
    try:
        yield deployment
    finally:
        await deployment.aclose()


@pytest.fixture
async def room(operator: AsyncClient, deployment: Deployment) -> OperatorRoom:
    return await OperatorRoom.invite(operator, bot_user_id=deployment.bot_user_id, check_alive=deployment.check_alive)


async def test_a_quiet_run_replies_once_to_every_message(deployment: Deployment, room: OperatorRoom) -> None:
    """The property the rest of this module asserts against a broken path, on an unbroken one.

    It is the control: same homeserver, same console process, same runner, same stub, so a failure
    in any of the others is the condition that test introduced rather than this harness. It also
    covers the other half of "answered exactly once" — a replayed frame or a re-sent answer would
    show up here as a duplicate.
    """
    await deployment.start_console("console-1")
    await deployment.serving()
    for body in ("one", "two", "three"):
        await room.say(body)
        await room.wait_for_reply(f"re: {body}")

    assert await room.replies() == ["re: one", "re: two", "re: three"]


async def test_a_reply_whose_send_is_refused_is_said_on_a_later_attempt(
    deployment: Deployment, room: OperatorRoom
) -> None:
    """A refused send used to make a whole turn silent, with the console still running.

    No reconnect, no roll, nothing queued: the delivery raised, `_deliver_reply` logged it and
    returned, `spoke = True` ran anyway, and the end-of-turn fallback that would have posted the
    same text off the `result` frame read "already said". The transcript row was written, so every
    surface except the room looked correct.

    Now the reply is a `session_outbox` row and the refusal is the row's: unsent, one attempt
    spent, retried after its backoff. Two refusals are armed rather than one, so both kinds of
    reply are covered — `two` is an ordinary assistant message, and `three`, which the agent
    answers with a `result` frame and no assistant message at all, is the row `_run_turn` mints
    off that frame.

    **Order is part of the assertion.** A refused reply holds the queue rather than being
    overtaken, so `four` — produced while the earlier two were still waiting out their backoff —
    still arrives last.
    """
    await deployment.start_console("console-1")
    session_id = await deployment.serving()
    await room.say("one")
    await room.wait_for_reply("re: one")

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
    """A produced reply the console had not yet said used to die with the process.

    `_deliver_reply` handed the answer to `pacer`, an in-process deque draining at
    `SENDS_PER_SECOND` — so with anything queued ahead of it, "delivered" was minutes from
    "spoken". Here the agent narrates 25 lines first, which is 25 paced notices, and the answer
    queues behind them. The console is then stopped the way a deploy stops it.

    What the answer is now is a row, so the assertion is in two halves and both matter: with the
    process *gone* the room still does not have it — a queue that had somehow flushed would make
    the second half vacuous — and a replacement console says it without the agent, the runner or
    the operator doing anything. The pacer's own shutdown flush is bounded at `FLUSH_SECONDS`,
    which at this rate is one send, so the gap is not luck.
    """
    await deployment.start_console("console-1")
    session_id = await deployment.serving()
    await room.say("one")
    await room.wait_for_reply("re: one")

    await room.say("two [narrate=25]")
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
    The console that took over used not to close it: the runner replays the frame and
    `RolloutRecorder.received` refuses it as one this session already has, and `adopt_open_turn`
    read the same row and resumed the turn with `spoke=True`, which its own docstring admits it
    cannot tell from delivered. So the `result` frame's copy of the text was suppressed too, and
    the answer ended up permanently recorded and permanently unspoken.

    **Exactly once, so this fails on a duplicate as well as on a drop** — which is the assertion
    the redrive has to survive. The row the first console wrote is the same row the second one
    drains, and re-deriving the same reply collides with it rather than adding a second copy.

    The agent holds its `result` across the roll on purpose: that leaves the turn open, so the
    second console adopts an exchange in flight rather than finding a finished one.
    """
    await deployment.start_console("console-1")
    session_id = await deployment.serving()
    await room.say("one")
    await room.wait_for_reply("re: one")

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
    """The ruling, end to end: nothing queues behind a running turn.

    The agent holds its answer to `two`, so `three` arrives while that turn is open. It is not
    delivered and not held — the room is told so while the turn is still running, and the operator
    sends it again once Haku is free. What proves it was a rejection rather than a delay is the
    reply list: `re: three` is there once, and only after the re-send.
    """
    await deployment.start_console("console-1")
    session_id = await deployment.serving()
    await room.say("one")
    await room.wait_for_reply("re: one")

    await room.say("two [hold]")
    await deployment.wait_until_holding()
    await room.say("three")
    await room.wait_for_notice("not delivered")

    deployment.release_the_agent()
    await deployment.wait_for_finished_turns(session_id, 2)
    await room.say("three")
    await room.wait_for_reply("re: three")

    assert await room.replies() == ["re: one", "re: two", "re: three"]


async def test_a_message_accepted_by_a_dying_session_is_not_offered_to_its_replacement(
    deployment: Deployment, room: OperatorRoom
) -> None:
    """The cost the rejection ruling accepts, pinned so it is a decision rather than a surprise.

    A killed sandbox leaves the session `ready` for a whole `ADOPTION_GRACE`, so `two` is
    **accepted** by a session that will never claim it. Its batch is acknowledged at that moment,
    so nothing offers it again: the replacement session answers the next thing said, and `two`
    survives only as a transcript row it is woken with (the test below).

    This is `message_drops.md` I3's window, deliberately reopened — R2.5's held batch is what used
    to close it, and holding is what the ruling removes.
    """
    await deployment.start_console("console-1")
    doomed = await deployment.serving()
    await room.say("one")
    await room.wait_for_reply("re: one")

    await deployment.kill_sandbox(doomed)
    await room.say("two")
    await deployment.wait_until_queued(doomed, "two")

    await deployment.serving(after=doomed)
    await room.say("three")
    await room.wait_for_reply("re: three")

    assert await room.replies() == ["re: one", "re: three"]


async def test_a_replacement_session_wakes_from_our_transcript_rather_than_from_the_room(
    deployment: Deployment, room: OperatorRoom
) -> None:
    """R3.3a, answered out of the console's own record: **which copy of the conversation is this?**

    The two copies are distinguishable here, which is why this can assert provenance rather than
    only content. Our transcript holds the operator's message as the wakeup ingress wrote —
    `[$event] one`, event id inline — while the homeserver holds an event whose body is `one` and
    whose id is a field beside it. So a prompt containing the first form was built from the
    transcript, and one built by paginating `/messages` could not contain it.

    The killed sandbox is how a replacement session gets made at all, and it also puts the
    load-bearing case in the same test: `two` is accepted by the dying session and never answered
    by it, so the only thing that can carry it into the replacement is the transcript.
    """
    await deployment.start_console("console-1")
    doomed = await deployment.serving()
    one = await room.say("one")
    await room.wait_for_reply("re: one")

    await deployment.kill_sandbox(doomed)
    two = await room.say("two")
    await deployment.wait_until_queued(doomed, "two")
    await deployment.serving(after=doomed)
    # The replacement launches its CLI on its first prompt, and the launch is what renders the
    # system prompt this test reads — so it has to be given something to answer. `two` is not
    # that something: it was accepted by the session that died, and nothing offers it again.
    await room.say("three")
    await room.wait_for_reply("re: three")

    launched = deployment.system_prompts()
    assert len(launched) >= 2, "no replacement session was ever started"
    woken = launched[-1]
    assert f"[{one}] one" in woken, "the operator's message, in the form only our record holds"
    assert "re: one" in woken, "half a conversation is not context — Haku's own reply is there too"
    assert f"[{two}] two" in woken, "the message its predecessor accepted and never answered"


if __name__ == "__main__":
    pytest_bazel.main()
