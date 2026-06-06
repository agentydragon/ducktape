"""Tests for AgentRegistry._collect_run (agent run finalization)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import cast
from uuid import UUID, uuid4

import pytest
import pytest_bazel

from props.core.ids import SnapshotSlug
from props.core.models.examples import WholeSnapshotExample
from props.core.oci_utils import RegistryProxyConfig
from props.db.config import DatabaseConfig
from props.db.database import Database
from props.db.models import AgentRun, AgentRunStatus
from props.orchestration.agent_registry import AgentRegistry
from props.orchestration.executor import ContainerExecutor, ContainerHandle, ContainerResult, Exited, PodInfo, TimedOut
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
        executor=cast(ContainerExecutor, _FakeExecutor()),
        db=db,
        db_config=DatabaseConfig(host="h", port=5432, database="d", user="u", password="p"),
        backend_url="http://backend",
        agent_base_env={},
        registry_config=RegistryProxyConfig(host="reg", port=8000),
        llm_base_url="http://proxy:8000",
    )


class _CapturingExecutor:
    env: dict[str, str] | None = None

    async def ensure_image(self, image_ref: str) -> str:
        return image_ref

    async def run_container(
        self,
        *,
        name: str,
        image_id: str,
        env: dict[str, str],
        labels: dict[str, str],
        annotations: dict[str, str] | None = None,
    ) -> ContainerHandle:
        self.env = env
        return _FakeHandle(name=name, result=ContainerResult(stdout="", stderr="", exit=Exited(exit_code=0)))

    async def list_pods(self, label_selector: dict[str, str]) -> list[PodInfo]:
        return []

    def handle_for(self, name: str) -> ContainerHandle:
        return _FakeHandle(name=name, result=ContainerResult(stdout="", stderr="", exit=Exited(exit_code=0)))

    async def close(self) -> None:
        pass


async def test_create_container_injects_agent_service_urls(db: Database) -> None:
    executor = _CapturingExecutor()
    registry = AgentRegistry(
        executor=cast(ContainerExecutor, executor),
        db=db,
        db_config=db.config,
        backend_url="http://backend",
        agent_base_env={},
        registry_config=RegistryProxyConfig(host="registry-proxy", port=8000),
        llm_base_url="http://llm-proxy:8000",
        agent_registry_url="http://agent-registry:8000",
    )

    await registry._create_container(uuid4(), image="registry-proxy/critic@sha256:abc", name="critic-env-test")

    assert executor.env is not None
    assert executor.env["PROPS_BACKEND_URL"] == "http://backend"
    assert executor.env["PROPS_REGISTRY_URL"] == "http://agent-registry:8000"
    assert executor.env["OPENAI_BASE_URL"] == "http://llm-proxy:8000/v1"


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


async def test_collect_run_finalizes_exited(db: Database) -> None:
    agent_run_id = _in_progress_critic_run(db)
    handle = _FakeHandle(
        name="critic-x",
        result=ContainerResult(stdout="boom\nTraceback ...", stderr="warn line", exit=Exited(exit_code=1)),
    )

    status = await _registry(db)._collect_run(agent_run_id, cast(ContainerHandle, handle), None)

    assert status == AgentRunStatus.EXITED
    assert handle.killed  # pod deleted before we finalized
    with db.session() as session:
        run = session.get(AgentRun, agent_run_id)
        assert run is not None
        assert run.status == AgentRunStatus.EXITED
        assert run.container_exit_code == 1


async def test_collect_run_finalizes_exit_zero(db: Database) -> None:
    agent_run_id = _in_progress_critic_run(db)
    handle = _FakeHandle(name="critic-y", result=ContainerResult(stdout="", stderr="", exit=Exited(exit_code=0)))

    status = await _registry(db)._collect_run(agent_run_id, cast(ContainerHandle, handle), None)

    assert status == AgentRunStatus.EXITED
    with db.session() as session:
        run = session.get(AgentRun, agent_run_id)
        assert run is not None
        assert run.container_exit_code == 0


async def test_collect_run_timeout_finalizes_timed_out(db: Database) -> None:
    agent_run_id = _in_progress_critic_run(db)
    handle = _FakeHandle(name="critic-z", result=ContainerResult(stdout="partial output", stderr="", exit=TimedOut()))

    status = await _registry(db)._collect_run(agent_run_id, cast(ContainerHandle, handle), 1)

    assert status == AgentRunStatus.TIMED_OUT
    with db.session() as session:
        run = session.get(AgentRun, agent_run_id)
        assert run is not None
        assert run.status == AgentRunStatus.TIMED_OUT
        assert run.container_exit_code is None


@dataclass
class _BlockingHandle:
    """Handle whose wait() blocks until the task is cancelled (mimics a long-running grader)."""

    name: str
    started: asyncio.Event
    killed: bool = False

    async def wait(self, *, timeout_seconds: int | None) -> ContainerResult:
        self.started.set()
        await asyncio.Event().wait()  # block forever — only a cancel unblocks us
        raise AssertionError("unreachable")

    async def kill_and_delete(self) -> None:
        self.killed = True


async def test_collect_run_cancellation_finalizes_as_cancelled(db: Database) -> None:
    # Reproduces the leak: the GraderSupervisor cancels a grader's task (via
    # AgentRunHandle.kill_and_delete) while it is waiting on the pod. Previously
    # the DB status update was skipped and the run leaked as IN_PROGRESS; now it
    # is finalized to CANCELLED.
    agent_run_id = _in_progress_critic_run(db)
    handle = _BlockingHandle(name="grader-x", started=asyncio.Event())

    task = asyncio.create_task(_registry(db)._collect_run(agent_run_id, cast(ContainerHandle, handle), None))
    await asyncio.wait_for(handle.started.wait(), timeout=5)  # ensure it is blocked in wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert handle.killed  # pod is still deleted on cancellation
    with db.session() as session:
        run = session.get(AgentRun, agent_run_id)
        assert run is not None
        assert run.status == AgentRunStatus.CANCELLED
        assert run.container_exit_code is None


if __name__ == "__main__":
    pytest_bazel.main()
