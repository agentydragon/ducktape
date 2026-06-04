"""Tests for AgentRegistry._collect_run (agent run finalization)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID, uuid4

import pytest_bazel

from props.core.ids import SnapshotSlug
from props.core.models.examples import WholeSnapshotExample
from props.core.oci_utils import RegistryProxyConfig
from props.db.config import DatabaseConfig
from props.db.database import Database
from props.db.models import AgentRun, AgentRunStatus
from props.orchestration.agent_registry import AgentRegistry
from props.orchestration.executor import ContainerResult, Exited, TimedOut
from props.testing.fixtures.runs import make_fake_critic_run


@dataclass
class _FakeHandle:
    """Test double for the container runtime — we exercise run finalization, not execution."""

    name: str
    result: ContainerResult
    killed: bool = False

    async def wait(self, *, timeout_seconds: int | None) -> ContainerResult:
        return self.result

    async def kill_and_delete(self) -> None:
        self.killed = True


class _FakeExecutor:
    async def close(self) -> None:
        pass


def _registry(db: Database) -> AgentRegistry:
    # Real DB; only the container runtime is faked. _collect_run touches just self._db.
    return AgentRegistry(
        executor=cast(Any, _FakeExecutor()),
        db=db,
        db_config=DatabaseConfig(host="h", port=5432, database="d", user="u", password="p"),
        backend_url="http://backend",
        agent_base_env={},
        registry_config=RegistryProxyConfig(host="reg", port=8000),
    )


def _in_progress_critic_run(db: Database) -> UUID:
    agent_run_id = uuid4()
    with db.session() as session:
        run = make_fake_critic_run(
            session=session,
            example=WholeSnapshotExample(snapshot_slug=SnapshotSlug("test/snap")),
            status=AgentRunStatus.IN_PROGRESS,
            agent_run_id=agent_run_id,
        )
        session.add(run)
        session.commit()
    return agent_run_id


async def test_collect_run_persists_container_logs(db: Database) -> None:
    agent_run_id = _in_progress_critic_run(db)
    handle = _FakeHandle(
        name="critic-x",
        result=ContainerResult(stdout="boom\nTraceback ...", stderr="warn line", exit=Exited(exit_code=1)),
    )

    status = await _registry(db)._collect_run(agent_run_id, cast(Any, handle), None)

    assert status == AgentRunStatus.EXITED
    assert handle.killed  # pod deleted before we persisted
    with db.session() as session:
        run = session.get(AgentRun, agent_run_id)
        assert run is not None
        assert run.status == AgentRunStatus.EXITED
        assert run.container_exit_code == 1
        # The captured logs are durable in the run record, not just the orchestrator's stdout.
        assert run.container_stdout == "boom\nTraceback ..."
        assert run.container_stderr == "warn line"


async def test_collect_run_stores_empty_logs_as_null(db: Database) -> None:
    agent_run_id = _in_progress_critic_run(db)
    handle = _FakeHandle(name="critic-y", result=ContainerResult(stdout="", stderr="", exit=Exited(exit_code=0)))

    await _registry(db)._collect_run(agent_run_id, cast(Any, handle), None)

    with db.session() as session:
        run = session.get(AgentRun, agent_run_id)
        assert run is not None
        assert run.container_exit_code == 0
        assert run.container_stdout is None
        assert run.container_stderr is None


async def test_collect_run_timeout_persists_partial_logs(db: Database) -> None:
    agent_run_id = _in_progress_critic_run(db)
    handle = _FakeHandle(name="critic-z", result=ContainerResult(stdout="partial output", stderr="", exit=TimedOut()))

    status = await _registry(db)._collect_run(agent_run_id, cast(Any, handle), 1)

    assert status == AgentRunStatus.TIMED_OUT
    with db.session() as session:
        run = session.get(AgentRun, agent_run_id)
        assert run is not None
        assert run.status == AgentRunStatus.TIMED_OUT
        assert run.container_exit_code is None
        assert run.container_stdout == "partial output"


if __name__ == "__main__":
    pytest_bazel.main()
