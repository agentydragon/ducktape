"""A Session built directly on a state directory, without a runner around it."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from agentplane.runner.adapter import HarnessAdapter
from agentplane.runner.config import RunnerConfig
from agentplane.runner.harness import Harness
from agentplane.runner.journal import Journal
from agentplane.runner.session import Session
from agentplane.runner.store import SessionRecord, SessionStore, StateOwner


def codex_record(
    workspace: Path, *, model: str = "test-model", total_context_budget_tokens: int | None = None
) -> SessionRecord:
    return SessionRecord(
        harness=Harness.CODEX,
        cwd=str(workspace),
        model=model,
        total_context_budget_tokens=total_context_budget_tokens,
        reasoning_effort="low",
        instructions="",
        event_source_id=uuid4(),
    )


@asynccontextmanager
async def open_session(
    session_id: str, record: SessionRecord, *, config: RunnerConfig, make_adapter: Callable[[Session], HarnessAdapter]
) -> AsyncIterator[Session]:
    """Store `record` under `config.state_dir` and open the Session over its journal, owning the state
    directory as a runner does."""
    store = SessionStore(config.state_dir / "sessions")
    owner = StateOwner(config.state_dir)
    store.write(session_id, record)
    try:
        async with Journal.open(store.directory(session_id) / "journal.sqlite", str(record.event_source_id)) as journal:
            yield Session(
                session_id,
                record=record,
                journal=journal,
                store=store,
                config=config,
                make_adapter=make_adapter,
                state_owner_descriptor=owner.descriptor,
                activity_changed=lambda: None,
            )
    finally:
        owner.close()
