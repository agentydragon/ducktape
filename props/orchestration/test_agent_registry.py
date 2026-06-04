"""Tests for AgentRegistry._collect_run (agent run finalization)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, cast
from uuid import uuid4

import pytest_bazel

from props.core.oci_utils import RegistryProxyConfig
from props.db.config import DatabaseConfig
from props.db.models import AgentRunStatus
from props.orchestration.agent_registry import AgentRegistry
from props.orchestration.executor import ContainerResult, Exited, TimedOut


@dataclass
class _FakeRun:
    """Stand-in for the AgentRun ORM row, with the fields _collect_run writes."""

    status: AgentRunStatus
    container_exit_code: int | None = None
    container_stdout: str | None = None
    container_stderr: str | None = None


@dataclass
class _FakeSession:
    run: _FakeRun

    def get(self, model: Any, key: Any) -> _FakeRun:
        return self.run

    def commit(self) -> None:
        pass


@dataclass
class _FakeDb:
    run: _FakeRun

    @contextmanager
    def session(self) -> Iterator[_FakeSession]:
        yield _FakeSession(self.run)


@dataclass
class _FakeHandle:
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


def _registry(db: _FakeDb) -> AgentRegistry:
    # Only `db` is exercised by _collect_run; the rest satisfy the constructor.
    return AgentRegistry(
        executor=cast(Any, _FakeExecutor()),
        db=cast(Any, db),
        db_config=DatabaseConfig(host="h", port=5432, database="d", user="u", password="p"),
        backend_url="http://backend",
        agent_base_env={},
        registry_config=RegistryProxyConfig(host="reg", port=8000),
    )


async def test_collect_run_persists_container_logs() -> None:
    run = _FakeRun(status=AgentRunStatus.IN_PROGRESS)
    handle = _FakeHandle(
        name="critic-x",
        result=ContainerResult(stdout="boom\nTraceback ...", stderr="warn line", exit=Exited(exit_code=1)),
    )
    status = await _registry(_FakeDb(run))._collect_run(uuid4(), cast(Any, handle), None)

    assert status == AgentRunStatus.EXITED
    assert run.status == AgentRunStatus.EXITED
    assert run.container_exit_code == 1
    # The captured logs land in the run record, not just the orchestrator's stdout.
    assert run.container_stdout == "boom\nTraceback ..."
    assert run.container_stderr == "warn line"
    assert handle.killed  # pod is deleted before we persist


async def test_collect_run_stores_empty_logs_as_null() -> None:
    run = _FakeRun(status=AgentRunStatus.IN_PROGRESS)
    handle = _FakeHandle(name="critic-y", result=ContainerResult(stdout="", stderr="", exit=Exited(exit_code=0)))
    await _registry(_FakeDb(run))._collect_run(uuid4(), cast(Any, handle), None)
    assert run.container_exit_code == 0
    assert run.container_stdout is None
    assert run.container_stderr is None


async def test_collect_run_timeout_persists_partial_logs() -> None:
    run = _FakeRun(status=AgentRunStatus.IN_PROGRESS)
    handle = _FakeHandle(name="critic-z", result=ContainerResult(stdout="partial output", stderr="", exit=TimedOut()))
    status = await _registry(_FakeDb(run))._collect_run(uuid4(), cast(Any, handle), 1)
    assert status == AgentRunStatus.TIMED_OUT
    assert run.status == AgentRunStatus.TIMED_OUT
    assert run.container_exit_code is None
    assert run.container_stdout == "partial output"


if __name__ == "__main__":
    pytest_bazel.main()
