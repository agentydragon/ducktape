"""The console's Claude bridge end to end: a real runner process on a real websocket.

Everything else covering this path calls `handle_runner` with a websocket stub and a scripted
`cli_over_websocket`, so the runner-facing route, the ASGI handshake, the version negotiation and
the envelope protocol only ever meet here. The runner is `//haku/runtime/x/bridge:runner_bin` as a
subprocess, the console is uvicorn on a real port, and the only stand-in is the `claude` binary.

The case is the one a console roll produces: a turn is in flight, the console goes away, the runner
redials the same address, and whichever console answers finishes the exchange the departed one
started. Each console builds its own store and listener because they stand for separate replicas,
and a store shared with the test would be one asyncpg pool driven from two event loops.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

import pytest_bazel
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from haku.console.chat_models import OPEN_SESSION_STATUSES, SPA_ORIGIN, SessionStatus
from haku.console.conversation_read_access import UnrestrictedReads
from haku.console.database_schema import SessionFrame
from haku.console.x import conversation_reads
from haku.console.x.conftest import configured_runtimes, runtime_config
from haku.console.x.conversation_reads import MessageEntry, PromptEntry, SetupOutputRecord, TurnAnsweredEnd
from haku.console.x.item_entries import entry_of
from haku.console.x.session_runtime import SessionService, internal_router
from haku.console.x.session_store import SessionStore
from haku.console.x.session_wakes import SessionWakes
from haku.console.x.setup_output import SETUP_OUTPUT_KIND
from haku.console.x.testing.recording_claims import RecordingClaims
from util.bazel.runfiles import get_required_path
from util.net import pick_free_port
from util.testing.asgi import serve_app

RUNNER_BIN = "_main/haku/runtime/x/bridge/runner_bin"

STUB_CLAUDE = "_main/haku/console/x/claude_code/testing/stub_claude_bin"

# The line the stub prints to stderr before any turn, which is what the sandbox narration this
# test reads back out of the rollout is made of.
GREETING = "the sandbox says hello"


def _console_app(database_url: str, workspace: Path) -> FastAPI:
    """One console replica: the runner route over its own engine, listener and service."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_async_engine(database_url, pool_pre_ping=True)
        session_wakes = SessionWakes(database_url)
        await session_wakes.start()
        claims = RecordingClaims()
        runtimes = configured_runtimes(claims, config=runtime_config(cwd=str(workspace)))
        store = SessionStore(async_sessionmaker(engine, expire_on_commit=False), runtimes)
        app.state.session_service = SessionService(runtimes, store, session_wakes)
        try:
            yield
        finally:
            await session_wakes.aclose()
            await engine.dispose()

    app = FastAPI(lifespan=lifespan)
    app.include_router(internal_router)
    return app


async def _runner_seqs(database_url: str, session_id: UUID) -> list[int]:
    """The runner's own numbers for one session's frames, in the order the console recorded them.

    Read off the rows because the question is about the column, which no reader exposes.
    """
    engine = create_async_engine(database_url)
    try:
        async with async_sessionmaker(engine)() as db:
            return list(
                await db.scalars(
                    select(SessionFrame.runner_seq)
                    .where(SessionFrame.session_id == session_id, SessionFrame.runner_seq.is_not(None))
                    .order_by(SessionFrame.frame_seq)
                )
            )
    finally:
        await engine.dispose()


async def _wait_until(
    what: str,
    ready: Callable[[], Awaitable[bool]],
    *,
    runner: asyncio.subprocess.Process,
    budget_seconds: float = 120.0,
) -> None:
    """Poll until *ready*, failing at once if the runner died rather than waiting the whole budget."""
    deadline = time.monotonic() + budget_seconds
    while not await ready():
        if runner.returncode is not None:
            raise AssertionError(f"the runner exited with status {runner.returncode} while waiting for {what}")
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.1)


async def test_a_real_runner_finishes_a_turn_the_console_that_started_it_never_saw_the_end_of(
    chat_store: SessionStore, migrated_db_url: str, operator_id: UUID, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    stub_state = tmp_path / "stub"
    stub_state.mkdir()
    view, token = await chat_store.create(operator_id)
    session_id = view.session_id
    # One port for both consoles: the runner redials the address its claim was created with, so
    # surviving a roll means surviving on that address rather than being told a new one.
    port = pick_free_port()

    runner = await asyncio.create_subprocess_exec(
        str(get_required_path(RUNNER_BIN)),
        "--harness",
        "claude",
        # The nested binary needs the test's RUNFILES_* to find its own runfiles, and the stub
        # inherits this environment in turn (`backend.child_environment`), which is how it learns
        # where to leave its handshake files. `HAKU_RUNNER_SETUP` stays unset: no bootstrap here.
        env=os.environ
        | {
            "HAKU_AGENT_SDK_RUNNER_WEBSOCKET_URL": f"ws://127.0.0.1:{port}/internal/claude/runner",
            "HAKU_RUNNER_SESSION_ID": str(session_id),
            "HAKU_AGENT_SDK_RUNNER_TOKEN": token,
            "HAKU_CLAUDE_PATH": str(get_required_path(STUB_CLAUDE)),
            "HAKU_STUB_STATE": str(stub_state),
            "HAKU_STUB_GREETING": GREETING,
        },
    )

    async def finished_turns() -> list[conversation_reads.TurnEnd | None]:
        turns = await chat_store.list_turns(session_id, cursor=None, limit=10, scope=UnrestrictedReads())
        return [turn.end for turn in sorted(turns, key=lambda turn: turn.started_at) if turn.ended_at]

    async def bridge_connected() -> bool:
        return await chat_store.status(session_id) == SessionStatus.READY

    async def first_turn_finished() -> bool:
        return len(await finished_turns()) == 1

    async def the_cli_has_the_second_prompt() -> bool:
        return (stub_state / "asked").exists()

    async def both_turns_finished() -> bool:
        return len(await finished_turns()) == 2

    try:
        async with serve_app(_console_app(migrated_db_url, workspace), port=port):
            await _wait_until("the runner's bridge handshake", bridge_connected, runner=runner)
            await chat_store.enqueue_prompt(operator_id, session_id, "first question", SPA_ORIGIN)
            await _wait_until("the first turn to finish", first_turn_finished, runner=runner)
            # `[hold]` is the stub's direction to answer and then wait, which is what strands this
            # turn in flight while the console below goes away.
            await chat_store.enqueue_prompt(operator_id, session_id, "second question [hold]", SPA_ORIGIN)
            await _wait_until("the CLI to receive the second prompt", the_cli_has_the_second_prompt, runner=runner)

        # The console is gone with the exchange unfinished. The sandbox is not: its CLI is still
        # holding an answer, which is the whole reason the runner outlives a connection.
        assert await chat_store.status(session_id) in OPEN_SESSION_STATUSES, "a roll is not a session ending"
        [in_flight] = [
            turn
            for turn in await chat_store.list_turns(session_id, cursor=None, limit=10, scope=UnrestrictedReads())
            if turn.ended_at is None
        ]

        async with serve_app(_console_app(migrated_db_url, workspace), port=port):
            (stub_state / "release").touch()
            await _wait_until("the adopted turn to finish", both_turns_finished, runner=runner)
    finally:
        if runner.returncode is None:
            runner.terminate()
        async with asyncio.timeout(30):
            await runner.wait()

    assert await finished_turns() == [TurnAnsweredEnd(), TurnAnsweredEnd()]
    turns = sorted(
        await chat_store.list_turns(session_id, cursor=None, limit=10, scope=UnrestrictedReads()),
        key=lambda turn: turn.started_at,
    )
    assert turns[1].turn_id == in_flight.turn_id, "the second console finished that turn rather than opening its own"
    rows = await chat_store.read_item_rows(
        await chat_store.conversation_of(session_id), after_seq=None, limit=100, scope=UnrestrictedReads()
    )
    assert [
        (entry.kind, entry.text) for entry in map(entry_of, rows) if isinstance(entry, PromptEntry | MessageEntry)
    ] == [
        ("prompt", "first question"),
        ("message", "re: first question"),
        ("prompt", "second question [hold]"),
        # Once, whether the departed console recorded the frame or the adopting one took it from
        # the runner's replay window.
        ("message", "re: second question"),
    ]
    # The sandbox's own account of itself, durable only because it is in the rollout: the pod's log
    # is reaped with the sandbox. Whole path — the CLI's stderr, the runner's forwarding, the
    # `setup_output` frame, and the transport reassembling it into a line.
    narration = await chat_store.read_frames(
        session_id, cursor=None, limit=10, kinds=[SETUP_OUTPUT_KIND], scope=UnrestrictedReads()
    )
    assert [frame.text for frame in narration if isinstance(frame, SetupOutputRecord)] == [GREETING]
    assert len(narration) == 1
    # The resume cursor, end to end. The second console computed it from the rows the first left
    # and sent it on `start`, so the runner replayed only what was above it. Without a cursor the
    # whole retained window comes back; runner position still makes every replay a no-op before it
    # reaches projection, including native classes with no payload-level id.
    numbered = await _runner_seqs(migrated_db_url, session_id)
    assert numbered == sorted(set(numbered)), "a frame was recorded twice, or out of the order it was sent in"
    assert await chat_store.highest_runner_seq(session_id) == numbered[-1]


if __name__ == "__main__":
    pytest_bazel.main()
