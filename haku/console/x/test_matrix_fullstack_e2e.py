"""The Matrix surface end to end, and the one question the operator actually asks of it:
**did every message I sent get an answer?**

Everything below this runs as itself. A real Synapse in a container, a console replica as its own
process (`testing/matrix_console_replica.py`) with the real `/sync` loop and session supervisor, a real
runner process per sandbox with a stub `claude` behind it, and a real Postgres under all of it.
The operator's side is driven straight through the client-server API, so what a test reads back is
the room, not the console's account of the room.

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
2. `_deliver_reply` only **queued**. `matrix_pacer` is an in-process deque draining at
   `SENDS_PER_SECOND`, so on a room with anything queued ahead of it "delivered" was minutes away
   from "spoken", and a console that stopped in between took the queue with it.
3. Across a roll neither was recovered. The frame is in `session_frames`, so the runner's replay
   is refused as one this session already has (`RolloutRecorder.received`), and `adopt_open_turn`
   read the same row as `spoke=True` — which its own docstring admits it cannot tell from
   delivered. Permanently recorded, permanently unspoken.

R11.6 says the opposite: "a produced reply must never be lost silently". What closes all three is
the durable room outbox (`matrix_outbox.py`): the reply is a row written with the message it
copies, and a drain says it and marks it sent only once the homeserver has taken it.
`test_a_quiet_run_replies_once_to_every_message` is the control, and it passed throughout.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from secrets import token_hex
from typing import IO, Any
from uuid import UUID

import pytest
import pytest_bazel

from haku.console.chat_models import SessionStatus
from haku.console.x.claude_chat import ASSISTANT_FRAME_KIND, SessionStore
from haku.console.x.testing.synapse_container import Account, Synapse, run_synapse
from util.bazel.runfiles import get_required_path
from util.net import pick_free_port
from util.testing.undeclared_outputs import undeclared_outputs_dir

CONSOLE_BIN = "_main/haku/console/x/testing/matrix_console_replica_bin"
RUNNER_BIN = "_main/haku/runtime/x/claude_bridge/runner_bin"
STUB_CLAUDE = "_main/haku/console/x/testing/stub_claude_bin"
SYSTEM_PROMPT_TEMPLATE = "_main/cluster/k8s/haku/console/matrix_system_prompt.md.j2"

PASSWORD = "not-a-secret"

# Generous, because what is waited for is a container, two processes and a long poll: these tests
# fail on what did not happen rather than on how fast it did not happen. Reaching it means
# something is wedged, and every process's log is in the undeclared outputs.
BUDGET_SECONDS = 180.0


class WedgedError(AssertionError):
    """Something the test was waiting for never happened, or a process it needed died first."""


class Room:
    """The operator's side of the one room Haku services."""

    def __init__(self, operator: Account, bot_user_id: str, room_id: str):
        self.room_id = room_id
        self._operator = operator
        self._bot = bot_user_id

    def say(self, body: str) -> str:
        return self._operator.send_text(self.room_id, body)

    def replies(self) -> list[str]:
        """What Haku said into the room, oldest first.

        `m.text` only: everything the console says *about* the conversation — joining, sandbox
        narration, the status line, lifecycle — is an `m.notice`, and an edit of one carries
        `m.new_content` rather than being a message of its own.
        """
        return [
            event["content"]["body"]
            for event in reversed(self._operator.messages(self.room_id))
            if event["type"] == "m.room.message"
            if event["sender"] == self._bot
            if event["content"].get("msgtype") == "m.text"
            if "m.new_content" not in event["content"]
        ]


def _assistant_text(payload: dict[str, Any] | None) -> str:
    blocks = (payload or {}).get("message", {}).get("content", [])
    return "".join(str(block.get("text", "")) for block in blocks if block.get("type") == "text")


class Deployment:
    """One console replica at a time, its sandboxes, and the database under them.

    The console is a process rather than an in-test app because these tests are about a console
    that goes away: `stop()` is the roll a deploy performs, and what a rolling replica does with
    what it had not finished saying is the whole subject.

    Sandboxes are started **here**, off the claim files the console writes, which is what a
    `SandboxClaim` controller does in production. It is also what makes a sandbox outlive the
    console rather than dying as its child — without which there would be no adoption to test.
    """

    def __init__(
        self, *, name: str, environment: dict[str, str], bot_user_id: str, port: int, state: Path, store: SessionStore
    ):
        self.bot_user_id = bot_user_id
        self._name = name
        self.stub_state = state / "stub"
        self.stub_state.mkdir()
        self.sessions: list[UUID] = []
        self._environment = environment
        self._port = port
        self._store = store
        self._claims = state / "claims"
        self._claims.mkdir()
        self._refusal = state / "refuse-next-reply"
        self._console: asyncio.subprocess.Process | None = None
        self._runners: dict[UUID, asyncio.subprocess.Process] = {}
        self._sandboxes: asyncio.Task[None] | None = None
        self._logs: list[IO[bytes]] = []

    async def _spawn(self, name: str, program: Path, environment: dict[str, str]) -> asyncio.subprocess.Process:
        """Run *program*, with its output in the undeclared outputs rather than in this test's.

        Both processes narrate continuously — every sync pass, every frame, every paced send — and
        a wedged test's explanation is in there, which pytest's captured output would lose when
        Bazel kills the target. Named per test, because every test in this module starts a
        `console-1` and one target's outputs are one directory.
        """
        log = (undeclared_outputs_dir() / f"{self._name}.{name}.log").open("wb")
        self._logs.append(log)
        return await asyncio.create_subprocess_exec(
            str(program), env=os.environ | self._environment | environment, stdout=log, stderr=log
        )

    async def start_console(self, name: str) -> None:
        assert self._console is None, "a replica is already running on this port"
        # HOSTNAME is what `claude_chat.REPLICA` reads and what the session lease records as its
        # holder: two replicas sharing one would make an adoption indistinguishable from the same
        # process reconnecting to itself, which is the thing under test.
        self._console = await self._spawn(name, get_required_path(CONSOLE_BIN), {"HOSTNAME": name})
        if self._sandboxes is None:
            self._sandboxes = asyncio.create_task(self._provision_sandboxes(), name="sandbox-controller")
        await self.wait_until("the console to listen", self._listening)

    async def stop(self) -> None:
        """End the replica the way a rolling deploy does, and wait until it is gone."""
        console, self._console = self._console, None
        assert console is not None, "no replica is running"
        console.terminate()  # SIGTERM, which is what Kubernetes sends a pod it is replacing.
        async with asyncio.timeout(120):
            await console.wait()

    async def serving(self) -> UUID:
        """Wait until the room has a sandbox behind it with the bridge up, and say which session.

        Also the barrier every test needs before its first message: a first `/sync` establishes a
        position rather than replaying backlog (R1.7a), so a message sent before the console has
        joined the room is one it is entitled never to see.
        """
        await self.wait_until("a session to be provisioned", self._provisioned)
        session_id = self.sessions[-1]
        await self.wait_until("the bridge to connect", lambda: self._ready(session_id))
        return session_id

    async def wait_for_reply(self, room: Room, body: str) -> None:
        await self.wait_until(f"{body!r} in the room", lambda: _says(room, body))

    async def wait_until_recorded(self, session_id: UUID, text: str) -> None:
        """Wait until *text* is a completed `assistant` frame in the session's rollout.

        This is the moment the console has the answer and has written it down — which every path
        downstream treats as interchangeable with the room having heard it.
        """

        async def recorded() -> bool:
            frames = await self._store.read_frames(session_id, after_seq=None, limit=200, kinds=[ASSISTANT_FRAME_KIND])
            return any(_assistant_text(frame.payload) == text for frame in frames if not frame.partial)

        await self.wait_until(f"{text!r} to be recorded in the rollout", recorded)

    async def wait_for_finished_turns(self, session_id: UUID, count: int) -> None:
        async def finished() -> bool:
            return len([turn for turn in await self._store.list_turns(session_id, limit=50) if turn.ended_at]) >= count

        await self.wait_until(f"{count} finished turn(s)", finished)

    def refuse_the_next_reply(self) -> None:
        """Arm one failed delivery, standing in for a homeserver that refuses one send.

        A rate limit past `MAX_RATE_LIMIT_RETRIES`, a transient 5xx, a room state that briefly
        forbids the send: none is reproducible on demand against a healthy Synapse, and what is
        under test is what the console does with a send that failed rather than how it failed.
        """
        self._refusal.write_text("")

    async def wait_until_refused(self) -> None:
        """Wait until the armed refusal has fired — the replica consuming the file is what says so."""
        await self.wait_until("the armed refusal to fire", lambda: _gone(self._refusal))

    async def wait_until_holding(self) -> None:
        await self.wait_until("the agent to hold its answer", lambda: _exists(self.stub_state / "asked"))

    def release_the_agent(self) -> None:
        (self.stub_state / "release").write_text("")

    async def wait_until(
        self, what: str, ready: Callable[[], Awaitable[bool]], *, budget: float = BUDGET_SECONDS
    ) -> None:
        deadline = time.monotonic() + budget
        while not await ready():
            self._living()
            if time.monotonic() > deadline:
                raise WedgedError(f"timed out waiting for {what}")
            await asyncio.sleep(0.2)

    def _living(self) -> None:
        """Fail now rather than at the deadline if what the test is waiting on has died."""
        if self._console is not None and self._console.returncode is not None:
            raise WedgedError(f"the console exited with status {self._console.returncode}")
        for session_id, runner in self._runners.items():
            if runner.returncode is not None:
                raise WedgedError(f"the runner for session {session_id} exited with status {runner.returncode}")
        if self._sandboxes is not None and self._sandboxes.done():
            raise WedgedError(f"nothing is provisioning sandboxes: {self._sandboxes.exception()}")

    async def _listening(self) -> bool:
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", self._port)
        except OSError:
            return False
        writer.close()
        return True

    async def _provisioned(self) -> bool:
        return bool(self.sessions)

    async def _ready(self, session_id: UUID) -> bool:
        return await self._store.status(session_id) == SessionStatus.READY

    async def _provision_sandboxes(self) -> None:
        """Start a runner for every claim the console writes, and stop one whose claim it deletes."""
        while True:
            claimed = {UUID(path.stem) for path in self._claims.glob("*.json")}
            for session_id in sorted(claimed - set(self._runners)):
                claim = json.loads((self._claims / f"{session_id}.json").read_text())
                self._runners[session_id] = await self._spawn(
                    f"runner-{len(self.sessions)}",
                    get_required_path(RUNNER_BIN),
                    {"HAKU_CLAUDE_SESSION_ID": str(session_id), "HAKU_AGENT_SDK_RUNNER_TOKEN": claim["bridge_token"]},
                )
                self.sessions.append(session_id)
            for session_id in set(self._runners) - claimed:
                self._runners.pop(session_id).terminate()
            await asyncio.sleep(0.1)

    async def aclose(self) -> None:
        if self._sandboxes is not None:
            self._sandboxes.cancel()
        running = [*self._runners.values(), *([self._console] if self._console is not None else [])]
        for process in running:
            if process.returncode is None:
                process.terminate()
        async with asyncio.timeout(60):
            for process in running:
                await process.wait()
        for log in self._logs:
            log.close()


async def _says(room: Room, body: str) -> bool:
    return body in room.replies()


async def _exists(path: Path) -> bool:
    return await asyncio.to_thread(path.exists)


async def _gone(path: Path) -> bool:
    return not await asyncio.to_thread(path.exists)


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
async def deployment(
    synapse: Synapse,
    operator: Account,
    migrated_db_url: str,
    chat_store: SessionStore,
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> AsyncIterator[Deployment]:
    bot_user_id = synapse.create_user(f"haku{token_hex(6)}", PASSWORD)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "deployment"
    state.mkdir()
    # One port across replicas: the runner redials the address its claim was created with, so
    # surviving a roll means surviving on that address rather than being told a new one.
    port = pick_free_port()

    deployment = Deployment(
        environment={
            "HAKU_E2E_DATABASE_URL": migrated_db_url,
            "HAKU_E2E_PORT": str(port),
            "HAKU_E2E_HOMESERVER": synapse.base_url,
            "HAKU_E2E_BOT_USER_ID": bot_user_id,
            "HAKU_E2E_BOT_PASSWORD": PASSWORD,
            "HAKU_E2E_OPERATOR_USER_ID": operator.user_id,
            "HAKU_E2E_WORKSPACE": str(workspace),
            "HAKU_E2E_CLAIMS_DIR": str(state / "claims"),
            "HAKU_E2E_REFUSE_NEXT_REPLY": str(state / "refuse-next-reply"),
            "HAKU_E2E_SYSTEM_PROMPT_TEMPLATE": str(get_required_path(SYSTEM_PROMPT_TEMPLATE)),
            # The nested binaries need the test's RUNFILES_* to find their own, and the stub
            # inherits this environment in turn (`backend.child_environment`), which is how
            # it learns where to leave its handshake files.
            "HAKU_AGENT_SDK_RUNNER_WEBSOCKET_URL": f"ws://127.0.0.1:{port}/internal/claude/runner",
            "HAKU_CLAUDE_PATH": str(get_required_path(STUB_CLAUDE)),
            "HAKU_STUB_STATE": str(state / "stub"),
        },
        name=request.node.name,
        bot_user_id=bot_user_id,
        port=port,
        state=state,
        store=chat_store,
    )
    try:
        yield deployment
    finally:
        await deployment.aclose()


@pytest.fixture
def room(operator: Account, deployment: Deployment) -> Room:
    """A room the operator invited Haku into. Haku joins it itself, on its own `/sync` (R3.6)."""
    return Room(operator, deployment.bot_user_id, operator.create_room(invite=deployment.bot_user_id))


async def test_a_quiet_run_replies_once_to_every_message(deployment: Deployment, room: Room) -> None:
    """The property the rest of this module asserts against a broken path, on an unbroken one.

    It is the control: same homeserver, same console process, same runner, same stub, so a failure
    in any of the others is the condition that test introduced rather than this harness. It also
    covers the other half of "answered exactly once" — a replayed frame or a re-sent answer would
    show up here as a duplicate.
    """
    await deployment.start_console("console-1")
    await deployment.serving()
    for body in ("one", "two", "three"):
        room.say(body)
        await deployment.wait_for_reply(room, f"re: {body}")

    assert room.replies() == ["re: one", "re: two", "re: three"]


async def test_a_reply_whose_send_is_refused_is_said_on_a_later_attempt(deployment: Deployment, room: Room) -> None:
    """A refused send used to make a whole turn silent, with the console still running.

    No reconnect, no roll, nothing queued: the delivery raised, `_deliver_reply` logged it and
    returned, `spoke = True` ran anyway, and the end-of-turn fallback that would have posted the
    same text off the `result` frame read "already said". The transcript row was written, so every
    surface except the room looked correct.

    Now the reply is a `session_outbox` row and the refusal is the row's: unsent, one attempt
    spent, retried after its backoff. Two refusals are armed rather than one, so both kinds of
    reply are covered — `two` is an ordinary assistant message, and `three`, which the agent
    answers with a `result` frame and no assistant message at all, is a turn's last word and
    carries no transcript row of its own.

    **Order is part of the assertion.** A refused reply holds the queue rather than being
    overtaken, so `four` — produced while the earlier two were still waiting out their backoff —
    still arrives last.
    """
    await deployment.start_console("console-1")
    session_id = await deployment.serving()
    room.say("one")
    await deployment.wait_for_reply(room, "re: one")

    deployment.refuse_the_next_reply()
    room.say("two")
    await deployment.wait_until_refused()
    await deployment.wait_for_finished_turns(session_id, 2)

    deployment.refuse_the_next_reply()
    room.say("three [silent]")
    await deployment.wait_until_refused()
    await deployment.wait_for_finished_turns(session_id, 3)

    # A message the room *can* answer, so what is asserted is a settled transcript rather than one
    # still in flight — and evidence that the console kept working after each dropped reply.
    room.say("four")
    await deployment.wait_for_reply(room, "re: four")

    assert room.replies() == ["re: one", "re: two", "re: three", "re: four"]


async def test_a_reply_still_queued_when_the_console_stops_is_said_by_its_replacement(
    deployment: Deployment, room: Room
) -> None:
    """A produced reply the console had not yet said used to die with the process.

    `_deliver_reply` handed the answer to `matrix_pacer`, an in-process deque draining at
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
    room.say("one")
    await deployment.wait_for_reply(room, "re: one")

    room.say("two [narrate=25]")
    await deployment.wait_until_recorded(session_id, "re: two")
    assert "re: two" not in room.replies(), "the answer reached the room before the gap could be opened"

    await deployment.stop()
    assert "re: two" not in room.replies(), "a room no console is serving gained a message"

    await deployment.start_console("console-2")
    await deployment.wait_for_reply(room, "re: two")

    assert room.replies() == ["re: one", "re: two"]


async def test_every_message_is_answered_exactly_once_across_a_console_roll(deployment: Deployment, room: Room) -> None:
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
    room.say("one")
    await deployment.wait_for_reply(room, "re: one")

    room.say("two [narrate=25] [hold]")
    await deployment.wait_until_holding()
    await deployment.wait_until_recorded(session_id, "re: two")
    assert "re: two" not in room.replies(), "the answer reached the room before the roll could be timed"

    await deployment.stop()
    await deployment.start_console("console-2")
    deployment.release_the_agent()
    await deployment.wait_for_finished_turns(session_id, 2)

    room.say("three")
    await deployment.wait_for_reply(room, "re: three")

    assert room.replies() == ["re: one", "re: two", "re: three"]


if __name__ == "__main__":
    pytest_bazel.main()
