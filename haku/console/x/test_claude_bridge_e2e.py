"""The console's Claude bridge end to end: a real runner process on a real websocket.

Everything else covering this path calls `handle_runner` with a websocket stub and replaces
`cli_over_websocket` with a scripted double, so the runner-facing route, the ASGI handshake, the
version negotiation and the envelope protocol were only ever exercised as halves that never met.
Here the runner is `//haku/runtime/x/claude_bridge:runner_bin` as a subprocess, the console is
uvicorn on a real port, and the only stand-in is the `claude` binary — a script that speaks the
CLI's newline-delimited JSON and nothing else.

The case is the one a console roll produces: a turn is in flight, the console goes away, the
runner redials the same address, and whichever console answers finishes the exchange the departed
one started. It is also why each console builds its own store and listener: they stand for
separate replicas, and a store shared with the test would be one asyncpg pool driven from two
event loops.
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
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from haku.console.chat_models import LIVE_SESSION_STATUSES, ChatMessageRole, ChatSessionStatus, TurnOutcome
from haku.console.x.chat_notifications import ChatNotifications
from haku.console.x.claude_chat import (
    SETUP_OUTPUT_KIND,
    ClaudeChatService,
    ClaudeChatStore,
    SpaSession,
    internal_router,
)
from haku.console.x.conftest import MCP_TOKEN, RecordingClaims, runtime_config
from util.bazel.runfiles import get_required_path
from util.net import pick_free_port
from util.testing.asgi import serve_app

RUNNER_BIN = "_main/haku/runtime/x/claude_bridge/runner_bin"

STUB_CLAUDE = "_main/haku/console/x/stub_claude.py"


def _console_app(database_url: str, workspace: Path) -> FastAPI:
    """One console replica: the runner route over its own engine, listener and service."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_async_engine(database_url, pool_pre_ping=True)
        notifications = ChatNotifications(database_url)
        await notifications.start()
        app.state.claude_chat_service = ClaudeChatService(
            # The deployed `cwd` is the sandbox's `/workspace`, which does not exist here — and the
            # runner starts the CLI in it, so the wrong one fails the launch rather than an
            # assertion.
            runtime_config(cwd=str(workspace)),
            ClaudeChatStore(async_sessionmaker(engine, expire_on_commit=False)),
            RecordingClaims(),
            notifications,
            mcp_token=MCP_TOKEN,
        )
        try:
            yield
        finally:
            await notifications.aclose()
            await engine.dispose()

    app = FastAPI(lifespan=lifespan)
    app.include_router(internal_router)
    return app


def _stub_claude(path: Path) -> Path:
    """Copy the stub out of runfiles and make it executable.

    The runner execs `HAKU_CLAUDE_PATH` directly, and a source file staged in runfiles does not
    reliably carry its executable bit.
    """
    path.write_text(get_required_path(STUB_CLAUDE).read_text())
    path.chmod(0o755)
    return path


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
    chat_store: ClaudeChatStore, migrated_db_url: str, operator_id: UUID, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    stub_state = tmp_path / "stub"
    stub_state.mkdir()
    view, token = await chat_store.create(operator_id, SpaSession())
    session_id = view.session_id
    # One port for both consoles: the runner redials the address its claim was created with, so
    # surviving a roll means surviving on that address rather than being told a new one.
    port = pick_free_port()

    runner = await asyncio.create_subprocess_exec(
        str(get_required_path(RUNNER_BIN)),
        # The nested binary needs the test's RUNFILES_* to find its own runfiles, and the stub
        # inherits this environment in turn (`runner.build_claude_environment`), which is how it
        # learns where to leave its handshake files. `HAKU_CLAUDE_SETUP` stays unset: there is no
        # sandbox bootstrap to run here.
        env=os.environ
        | {
            "HAKU_AGENT_SDK_RUNNER_WEBSOCKET_URL": f"ws://127.0.0.1:{port}/internal/claude/runner",
            "HAKU_CLAUDE_SESSION_ID": str(session_id),
            "HAKU_AGENT_SDK_RUNNER_TOKEN": token,
            "HAKU_CLAUDE_PATH": str(_stub_claude(tmp_path / "claude")),
            "HAKU_STUB_STATE": str(stub_state),
        },
    )

    async def finished_turns() -> list[str | None]:
        turns = await chat_store.list_turns(session_id, limit=10)
        return [turn.outcome for turn in sorted(turns, key=lambda turn: turn.started_at) if turn.ended_at]

    async def bridge_connected() -> bool:
        return await chat_store.status(session_id) == ChatSessionStatus.READY

    async def first_turn_finished() -> bool:
        return len(await finished_turns()) == 1

    async def the_cli_has_the_second_prompt() -> bool:
        return (stub_state / "asked").exists()

    async def both_turns_finished() -> bool:
        return len(await finished_turns()) == 2

    try:
        async with serve_app(_console_app(migrated_db_url, workspace), port=port):
            await _wait_until("the runner's bridge handshake", bridge_connected, runner=runner)
            await chat_store.enqueue_prompt(operator_id, session_id, "first question")
            await _wait_until("the first turn to finish", first_turn_finished, runner=runner)
            await chat_store.enqueue_prompt(operator_id, session_id, "second question")
            await _wait_until("the CLI to receive the second prompt", the_cli_has_the_second_prompt, runner=runner)

        # The console is gone with the exchange unfinished. The sandbox is not: its CLI is still
        # holding an answer, which is the whole reason the runner outlives a connection.
        assert await chat_store.status(session_id) in LIVE_SESSION_STATUSES, "a roll is not a session ending"
        [in_flight] = [turn for turn in await chat_store.list_turns(session_id, limit=10) if turn.ended_at is None]

        async with serve_app(_console_app(migrated_db_url, workspace), port=port):
            (stub_state / "release").touch()
            await _wait_until("the adopted turn to finish", both_turns_finished, runner=runner)
    finally:
        runner.terminate()
        async with asyncio.timeout(30):
            await runner.wait()

    assert await finished_turns() == [TurnOutcome.ANSWERED, TurnOutcome.ANSWERED]
    turns = sorted(await chat_store.list_turns(session_id, limit=10), key=lambda turn: turn.started_at)
    assert turns[1].turn_id == in_flight.turn_id, "the second console finished that turn rather than opening its own"
    # Cost lives only on the `result` frame, so this is that frame having crossed the real bridge.
    assert [turn.cost_usd for turn in turns] == [0.01, 0.01]
    conversation = await chat_store.get(operator_id, session_id)
    assert [(message.role, message.content) for message in conversation.messages] == [
        (ChatMessageRole.USER, "first question"),
        (ChatMessageRole.ASSISTANT, "answer 1"),
        (ChatMessageRole.USER, "second question"),
        # Once, whether the departed console recorded the frame or the adopting one took it from
        # the runner's replay window.
        (ChatMessageRole.ASSISTANT, "answer 2"),
    ]
    # The sandbox's own account of itself, which is only durable because it is in the rollout —
    # the pod's log is reaped with the sandbox. Whole path: the CLI's stderr, the runner's
    # forwarding, the `setup_output` frame, and the transport reassembling it into a line.
    narration = await chat_store.read_frames(session_id, after_seq=None, limit=10, kinds=[SETUP_OUTPUT_KIND])
    assert [frame.payload for frame in narration] == [{"kind": SETUP_OUTPUT_KIND, "text": "the sandbox says hello"}]


if __name__ == "__main__":
    pytest_bazel.main()
