"""One console replica at a time, its sandboxes, and the database under them.

The console is a process rather than an in-test app because the tests this serves are about a
console that goes away: `stop()` is the roll a deploy performs.

Sandboxes are started **here**, off the claim files the console writes, which is what a
`SandboxClaim` controller does in production. It is also what makes a sandbox outlive the console
rather than dying as its child — without which there would be no adoption to test.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path
from typing import IO
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.conversation.item_vocabulary import ItemType
from haku.console.conversation_read_access import UnrestrictedReads
from haku.console.database_schema import ConversationItem, MatrixSyncWatermark, Session, SubmittedPrompt
from haku.console.session.status import SessionStatus
from haku.console.session.store import Store
from haku.console.x.testing.waiting import BUDGET_SECONDS, WedgedError, wait_until
from util.bazel.runfiles import get_required_path
from util.net import pick_free_port
from util.testing.undeclared_outputs import undeclared_outputs_dir

CONSOLE_BIN = "_main/haku/console/channels/matrix/testing/console_replica_bin"
RUNNER_BIN = "_main/haku/runner/runner_bin"

# The two production windows a killed sandbox has to pass through before a replacement may adopt
# it, shortened for the full-stack tests. Nothing here changes which path runs — the sweep still
# has to observe a lapsed lease and mint the replacement — only how long the wall clock takes to
# get there, and at 45 s + 10 s that was two thirds of this suite's runtime.
#
# The grace has a floor as well as a ceiling: `kill_sandbox` then a message must still land
# *inside* the window, because "accepted by a session that will never claim it" is the state two
# of these tests are about. Five seconds is generous for one Matrix round trip and a queued
# prompt, while a lapse the sweep can see within a second of it.
ADOPTION_GRACE = timedelta(seconds=5)
SWEEP_INTERVAL = timedelta(seconds=1)
STUB_CLAUDE = "_main/haku/console/x/claude_code/testing/stub_claude_bin"
SYSTEM_PROMPT_TEMPLATE = "_main/cluster/k8s/haku/console/haku_system_prompt.md.j2"


async def _exists(path: Path) -> bool:
    return await asyncio.to_thread(path.exists)


async def _gone(path: Path) -> bool:
    return not await asyncio.to_thread(path.exists)


class Deployment:
    """The console replicas serving one room, and everything the test needs to outlive them."""

    def __init__(
        self,
        *,
        name: str,
        homeserver: str,
        bot_user_id: str,
        bot_password: str,
        operator_user_id: str,
        database_url: str,
        sessions: async_sessionmaker[AsyncSession],
        store: Store,
        state: Path,
    ):
        self.bot_user_id = bot_user_id
        self._name = name
        self._store = store
        self._db = sessions
        # Sandboxes this test killed on purpose, so their exit is not read as one that wedged it.
        self._abandoned: set[UUID] = set()
        self._session_ids: list[UUID] = []
        self._console: asyncio.subprocess.Process | None = None
        self._runners: dict[UUID, asyncio.subprocess.Process] = {}
        self._sandboxes: asyncio.Task[None] | None = None
        self._logs: list[IO[bytes]] = []
        self.stub_state = state / "stub"
        self.stub_state.mkdir()
        self._claims = state / "claims"
        self._claims.mkdir()
        workspace = state / "workspace"
        workspace.mkdir()
        self._refusal = state / "refuse-next-reply"
        # One port across replicas: the runner redials the address its claim was created with, so
        # surviving a roll means surviving on that address rather than being told a new one.
        self._port = pick_free_port()
        self._environment = {
            "HAKU_E2E_DATABASE_URL": database_url,
            "HAKU_E2E_PORT": str(self._port),
            "HAKU_E2E_HOMESERVER": homeserver,
            "HAKU_E2E_BOT_USER_ID": bot_user_id,
            "HAKU_E2E_BOT_PASSWORD": bot_password,
            "HAKU_E2E_OPERATOR_USER_ID": operator_user_id,
            "HAKU_E2E_WORKSPACE": str(workspace),
            "HAKU_E2E_CLAIMS_DIR": str(self._claims),
            "HAKU_E2E_REFUSE_NEXT_REPLY": str(self._refusal),
            "HAKU_E2E_SYSTEM_PROMPT_TEMPLATE": str(get_required_path(SYSTEM_PROMPT_TEMPLATE)),
            "HAKU_E2E_ADOPTION_GRACE_SECONDS": str(ADOPTION_GRACE.total_seconds()),
            "HAKU_E2E_SWEEP_INTERVAL_SECONDS": str(SWEEP_INTERVAL.total_seconds()),
            # The nested binaries need the test's RUNFILES_* to find their own, and the stub
            # inherits this environment in turn (`backend.child_environment`), which is how
            # it learns where to leave its handshake files.
            "HAKU_AGENT_SDK_RUNNER_WEBSOCKET_URL": f"ws://127.0.0.1:{self._port}/internal/claude/runner",
            "HAKU_CLAUDE_PATH": str(get_required_path(STUB_CLAUDE)),
            "HAKU_STUB_STATE": str(self.stub_state),
        }

    async def _spawn(
        self, name: str, program: Path, environment: dict[str, str], *arguments: str
    ) -> asyncio.subprocess.Process:
        """Run *program*, with its output in the undeclared outputs rather than in this test's.

        Both processes narrate continuously, and a wedged test's explanation is in there — which
        pytest's captured output would lose when Bazel kills the target. Named per test, because
        every test using this starts a `console-1` and one target's outputs are one directory.
        """
        log = (undeclared_outputs_dir() / f"{self._name}.{name}.log").open("wb")
        self._logs.append(log)
        return await asyncio.create_subprocess_exec(
            str(program), *arguments, env=os.environ | self._environment | environment, stdout=log, stderr=log
        )

    async def start_console(self, name: str) -> None:
        assert self._console is None, "a replica is already running on this port"
        # HOSTNAME is what `session_runtime.REPLICA` reads and what the session lease records as its
        # holder: two replicas sharing one would make an adoption indistinguishable from the same
        # process reconnecting to itself, which is the thing under test.
        self._console = await self._spawn(name, get_required_path(CONSOLE_BIN), {"HOSTNAME": name})
        if self._sandboxes is None:
            self._sandboxes = asyncio.create_task(self._provision_sandboxes(), name="sandbox-controller")
        await self._wait_until("the console to listen", self._listening)

    async def stop(self) -> None:
        """End the replica the way a rolling deploy does, and wait until it is gone."""
        console, self._console = self._console, None
        assert console is not None, "no replica is running"
        console.terminate()  # SIGTERM, which is what Kubernetes sends a pod it is replacing.
        async with asyncio.timeout(120):
            await console.wait()

    async def kill_sandbox(self, session_id: UUID) -> None:
        """Kill the sandbox behind *session_id* and leave it dead, with the console still running.

        `SIGKILL` so no finalizer runs, which is the sandbox loss `expire_stale_leases` is the only
        observer of. The console reads the dropped socket as a lease handed back, so the session's
        row stays `ready` and adoptable for a whole `ADOPTION_GRACE` — and a message sent into that
        window is accepted by a session that will never claim it.
        """
        self._abandoned.add(session_id)
        runner = self._runners[session_id]
        runner.kill()
        await runner.wait()

    def system_prompts(self) -> list[str]:
        """What each CLI this deployment launched was woken with, in launch order.

        Written by the stub out of its own argv, because nothing else can see it: the console
        renders the prompt into the launch envelope, and the runner ignores a launch for a process
        it is already holding.
        """
        recorded = self.stub_state / "system-prompts.jsonl"
        return [json.loads(line) for line in recorded.read_text().splitlines()] if recorded.exists() else []

    async def wait_until_queued(self, session_id: UUID, body: str) -> None:
        """Wait until *body* is a pending inbox prompt of *session_id*'s conversation.

        The premise of the test that kills a sandbox: a message merely refused and held by the
        homeserver would be answered by the replacement whatever the watermark did, and the test
        would pass without exercising anything. Acceptance leaves a durable `submitted_prompt`
        (#4667) — there is no transcript item until some session admits it, and the doomed one
        never will.
        """

        async def queued() -> bool:
            async with self._db() as db:
                prompts = await db.scalars(
                    select(SubmittedPrompt.text)
                    .join(Session, Session.conversation_id == SubmittedPrompt.conversation_id)
                    .where(
                        Session.session_id == session_id,
                        SubmittedPrompt.admitted_at.is_(None),
                        SubmittedPrompt.withdrawn_at.is_(None),
                    )
                )
            return any(body in prompt for prompt in prompts)

        await self._wait_until(f"{body!r} to be queued against session {session_id}", queued)

    async def sync_position(self) -> str:
        """Where the sync loop has acknowledged up to, as a test can put it back."""
        async with self._db() as db:
            position = await db.scalar(select(MatrixSyncWatermark.next_batch))
        assert position is not None, "nothing has been acknowledged yet"
        return str(position)

    async def rewind_sync_to(self, position: str) -> None:
        """Put the acknowledged position back, which is what a crash before the commit leaves.

        The one gap the watermark cannot express: a prompt commits, the process dies, and the
        homeserver is still holding the batch that produced it. Only safe with the console stopped
        — a running sync pass writes its own position over this one.
        """
        async with self._db.begin() as db:
            await db.execute(update(MatrixSyncWatermark).values(next_batch=position))

    async def serving(self, *, after: UUID | None = None) -> UUID:
        """Wait until the room has a sandbox behind it with the bridge up, and say which session.

        Lazy sessions have no sandbox before their first prompt, so this cannot be the pre-prompt
        sync barrier. Full-stack callers first wait for the room-joined notice, then send the
        prompt whose durable demand creates the session and claim, and only then wait here for its
        runner.

        `after` names a session being replaced. Neutral supervision mints the replacement only once
        durable demand exists and the dead one's lease has lapsed, so without it a test that has
        just killed a sandbox reads its own victim back and believes the replacement is already
        serving.
        """
        await self._wait_until("a session to be provisioned", lambda: self._provisioned(after))
        session_id = self._session_ids[-1]
        await self._wait_until("the bridge to connect", lambda: self._ready(session_id))
        return session_id

    async def wait_until_recorded(self, session_id: UUID, text: str) -> None:
        """Wait until *text* is durable neutral conversation content for this session."""

        async def recorded() -> bool:
            async with self._db() as db:
                return (
                    await db.scalar(
                        select(ConversationItem.item_id)
                        .where(
                            ConversationItem.session_id == session_id,
                            ConversationItem.item_type == ItemType.MESSAGE,
                            ConversationItem.item_text == text,
                        )
                        .limit(1)
                    )
                ) is not None

        await self._wait_until(f"{text!r} to be recorded in the rollout", recorded)

    async def wait_for_finished_turns(self, session_id: UUID, count: int) -> None:
        async def finished() -> bool:
            return (
                len(
                    [
                        turn
                        for turn in await self._store.list_turns(
                            session_id, cursor=None, limit=50, scope=UnrestrictedReads()
                        )
                        if turn.ended_at
                    ]
                )
                >= count
            )

        await self._wait_until(f"{count} finished turn(s)", finished)

    def refuse_the_next_reply(self) -> None:
        """Arm one failed delivery, standing in for a homeserver that refuses one send.

        A rate limit past `MAX_RATE_LIMIT_RETRIES`, a transient 5xx, a room state that briefly
        forbids the send: none is reproducible on demand against a healthy Synapse, and what is
        under test is what the console does with a failed send rather than how it failed.
        """
        self._refusal.write_text("")

    async def wait_until_refused(self) -> None:
        """Wait until the armed refusal has fired — the replica consuming the file is what says so."""
        await self._wait_until("the armed refusal to fire", lambda: _gone(self._refusal))

    async def wait_until_holding(self) -> None:
        await self._wait_until("the agent to hold its answer", lambda: _exists(self.stub_state / "asked"))

    def release_the_agent(self) -> None:
        (self.stub_state / "release").write_text("")

    def check_alive(self) -> None:
        """Fail now rather than at the deadline if what a test is waiting on has died."""
        if self._console is not None and self._console.returncode is not None:
            raise WedgedError(f"the console exited with status {self._console.returncode}")
        for session_id, runner in self._runners.items():
            if runner.returncode is not None and session_id not in self._abandoned:
                raise WedgedError(f"the runner for session {session_id} exited with status {runner.returncode}")
        if self._sandboxes is not None and self._sandboxes.done():
            raise WedgedError(f"nothing is provisioning sandboxes: {self._sandboxes.exception()}")

    async def _wait_until(
        self, what: str, ready: Callable[[], Awaitable[bool]], *, budget: float = BUDGET_SECONDS
    ) -> None:
        await wait_until(what, ready, check_alive=self.check_alive, budget=budget)

    async def _listening(self) -> bool:
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", self._port)
        except OSError:
            return False
        writer.close()
        return True

    async def _provisioned(self, after: UUID | None = None) -> bool:
        return bool(self._session_ids) and self._session_ids[-1] != after

    async def _ready(self, session_id: UUID) -> bool:
        return await self._store.status(session_id) == SessionStatus.READY

    async def _provision_sandboxes(self) -> None:
        """Start a runner for every claim the console writes, and stop one whose claim it deletes."""
        while True:
            claimed = {UUID(path.stem) for path in self._claims.glob("*.json")}
            for session_id in sorted(claimed - set(self._runners)):
                claim = json.loads((self._claims / f"{session_id}.json").read_text())
                self._runners[session_id] = await self._spawn(
                    f"runner-{len(self._session_ids)}",
                    get_required_path(RUNNER_BIN),
                    {"HAKU_RUNNER_SESSION_ID": str(session_id), "HAKU_RUNNER_TOKEN": claim["bridge_token"]},
                    "--harness",
                    "claude",
                )
                self._session_ids.append(session_id)
            for session_id in set(self._runners) - claimed:
                # Guarded, not assumed alive: a test may already have killed this one, and
                # `terminate()` on a reaped process raises and would take this loop with it.
                if (runner := self._runners.pop(session_id)).returncode is None:
                    runner.terminate()
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
