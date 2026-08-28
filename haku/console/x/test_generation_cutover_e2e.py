"""The post-cut full stack, end to end — the operator's health gate for the generation window.

A real runner process (`//haku/runtime/x/bridge:runner_bin`) over a real websocket to a real Console
journal handler, with the stub `claude` as the only stand-in — replacing the v3 bridge e2e, which
went with the fold it pinned. This is stage 4's health gate
(#4667 comment 5422375226 step 5): the same flow the operator runs after rolling the images to
prove the cut — handshake, a streamed message carried as journal batches, a tool call and its
result, prompt admission materialising the authored item, cumulative ACK, and a console restart the
runner resumes across.

The migration to head has already cut the generation; the gate establishes a session and drives it
exactly as the runbook says.
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

from haku.console.chat_models import SPA_ORIGIN, SessionStatus
from haku.console.conversation_read_access import UnrestrictedReads
from haku.console.database_schema import Session, SubmittedPrompt
from haku.console.x.conftest import configured_runtimes, runtime_config
from haku.console.x.conversation_reads import MessageEntry, PromptEntry, ToolCallEntry, TurnAnsweredEnd
from haku.console.x.item_entries import entry_of
from haku.console.x.session_runtime import SessionService, internal_router
from haku.console.x.session_store import SessionStore
from haku.console.x.session_wakes import SessionWakes
from haku.console.x.testing.recording_claims import RecordingClaims
from util.bazel.runfiles import get_required_path
from util.net import pick_free_port
from util.testing.asgi import serve_app

RUNNER_BIN = "_main/haku/runtime/x/bridge/runner_bin"
STUB_CLAUDE = "_main/haku/console/x/claude_code/testing/stub_claude_bin"


def _console_app(database_url: str, workspace: Path) -> FastAPI:
    """One journal-generation console replica: the runner route over its own engine and service."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_async_engine(database_url, pool_pre_ping=True)
        session_wakes = SessionWakes(database_url)
        await session_wakes.start()
        runtimes = configured_runtimes(RecordingClaims(), config=runtime_config(cwd=str(workspace)))
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


async def _wait_until(
    what: str,
    ready: Callable[[], Awaitable[bool]],
    *,
    runner: asyncio.subprocess.Process,
    budget_seconds: float = 120.0,
) -> None:
    deadline = time.monotonic() + budget_seconds
    while not await ready():
        if runner.returncode is not None:
            raise AssertionError(f"the runner exited with status {runner.returncode} while waiting for {what}")
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.1)


async def _transcript(store: SessionStore, conversation_id: UUID) -> list[tuple[str, str]]:
    """The prompt/message transcript pairs and the tool call, read through the store's reader."""
    rows = await store.read_item_rows(conversation_id, after_seq=None, limit=100, scope=UnrestrictedReads())
    out: list[tuple[str, str]] = []
    for entry in map(entry_of, rows):
        if isinstance(entry, PromptEntry | MessageEntry):
            out.append((entry.kind, entry.text))
        elif isinstance(entry, ToolCallEntry):
            out.append((entry.kind, f"{entry.tool_name}:{entry.outcome}"))
    return out


async def _acked_and_admission(database_url: str, session_id: UUID, conversation_id: UUID) -> tuple[int, bool]:
    engine = create_async_engine(database_url)
    try:
        async with async_sessionmaker(engine)() as db:
            acked = await db.scalar(select(Session.acked_batch_seq).where(Session.session_id == session_id))
            admitted = await db.scalar(
                select(SubmittedPrompt.admitted_at).where(SubmittedPrompt.conversation_id == conversation_id)
            )
            return int(acked or 0), admitted is not None
    finally:
        await engine.dispose()


async def test_the_cut_stack_answers_a_prompt_with_a_tool_call_over_the_journal(
    session_store: SessionStore, migrated_db_url: str, operator_id: UUID, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    stub_state = tmp_path / "stub"
    stub_state.mkdir()

    view, token = await session_store.create(operator_id)
    session_id = view.session_id
    conversation_id = await session_store.conversation_of(session_id)
    port = pick_free_port()

    runner = await asyncio.create_subprocess_exec(
        str(get_required_path(RUNNER_BIN)),
        "--harness",
        "claude",
        env=os.environ
        | {
            "HAKU_AGENT_SDK_RUNNER_WEBSOCKET_URL": f"ws://127.0.0.1:{port}/internal/claude/runner",
            "HAKU_RUNNER_SESSION_ID": str(session_id),
            "HAKU_AGENT_SDK_RUNNER_TOKEN": token,
            "HAKU_CLAUDE_PATH": str(get_required_path(STUB_CLAUDE)),
            "HAKU_STUB_STATE": str(stub_state),
        },
    )

    async def finished_ends() -> list[object]:
        turns = await session_store.list_turns(session_id, cursor=None, limit=10, scope=UnrestrictedReads())
        return [turn.end for turn in sorted(turns, key=lambda turn: turn.started_at) if turn.ended_at]

    async def bridge_ready() -> bool:
        return await session_store.status(session_id) == SessionStatus.READY

    async def first_turn_finished() -> bool:
        return len(await finished_ends()) == 1

    try:
        async with serve_app(_console_app(migrated_db_url, workspace), port=port):
            await _wait_until("the runner's journal handshake", bridge_ready, runner=runner)
            # Submit through the inbox and let the runner dispatch/admit it — the whole prompt path.
            await session_store.submit_prompt(operator_id, conversation_id, "hello [tool=echo]", SPA_ORIGIN)
            await _wait_until("the first turn to finish", first_turn_finished, runner=runner)

            # The full stack: the prompt was admitted into the transcript, the tool call and its
            # result were projected by the runner and committed by the journal consumer, and the
            # streamed message concatenated to its answer. The prompt entry keeps the stub's
            # `[tool=echo]` stage direction: the item is materialised from the Console's own
            # `submitted_prompt` row, verbatim — the stub strips directives only from its answer.
            assert await _transcript(session_store, conversation_id) == [
                ("prompt", "hello [tool=echo]"),
                ("tool_call", "echo:succeeded"),
                ("message", "re: hello"),
            ]
            acked, admitted = await _acked_and_admission(migrated_db_url, session_id, conversation_id)
            assert acked > 0, "the console acknowledged at least one batch"
            assert admitted, "the submitted prompt was marked admitted"
            assert await finished_ends() == [TurnAnsweredEnd()]

        # A console roll: the sandbox and its runner outlive the socket. A fresh console resumes the
        # journal from the durable cursor and answers a second prompt — the reconnect path the gate
        # depends on.
        assert await session_store.status(session_id) in {SessionStatus.READY, SessionStatus.PROVISIONING}
        async with serve_app(_console_app(migrated_db_url, workspace), port=port):
            await _wait_until("the runner to redial the new console", bridge_ready, runner=runner)
            await session_store.submit_prompt(operator_id, conversation_id, "again", SPA_ORIGIN)
            await _wait_until(
                "the second turn to finish", lambda: _has_n_finished(session_store, session_id, 2), runner=runner
            )
    finally:
        if runner.returncode is None:
            runner.terminate()
        async with asyncio.timeout(30):
            await runner.wait()

    assert await finished_ends() == [TurnAnsweredEnd(), TurnAnsweredEnd()]
    transcript = await _transcript(session_store, conversation_id)
    assert transcript.count(("message", "re: again")) == 1, "the second answer was recorded exactly once"


async def _has_n_finished(store: SessionStore, session_id: UUID, n: int) -> bool:
    turns = await store.list_turns(session_id, cursor=None, limit=10, scope=UnrestrictedReads())
    return len([turn for turn in turns if turn.ended_at]) == n


if __name__ == "__main__":
    pytest_bazel.main()
