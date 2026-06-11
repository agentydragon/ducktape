from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from experimental.ember_evals.steps import (
    EvalResult,
    ExpectMatrixReplyResult,
    KillProcessResult,
    ProbeHttpResult,
    ScenarioResult,
    SendMatrixMessageResult,
    SnapshotWorkspaceResult,
    StepErrorResult,
    StepResult,
    StepSkippedResult,
    StepStatus,
    VerifyFileContainsResult,
    VerifyFileContentsResult,
    WaitForMatrixResponseResult,
    WaitSecondsResult,
)
from pydantic import BaseModel

from experimental.ember_evals.executor import ScenarioExecutionError, ScenarioSkippedError

if TYPE_CHECKING:
    from experimental.ember_evals.executor import ScenarioExecutor


class Scenario(ABC):
    """Base class for a single evaluation scenario with built-in helpers."""

    id: str
    description: str | None = None

    def __init__(self, executor: ScenarioExecutor, scenario_dir: Path) -> None:
        self._executor = executor
        self._scenario_dir = scenario_dir
        self._results: list[StepResult] = []

    # -- lifecycle hooks -------------------------------------------------
    async def setup(self) -> None:
        return

    @abstractmethod
    async def run(self) -> None:
        """Execute the scenario's logic."""

    async def teardown(self, result: ScenarioResult) -> None:
        return

    async def execute(self) -> None:
        """Default lifecycle runner that subclasses may override if needed."""
        await self.setup()
        await self.run()

    # -- executor wiring -------------------------------------------------
    @property
    def executor(self) -> ScenarioExecutor:
        return self._executor

    @property
    def scenario_dir(self) -> Path:
        return self._scenario_dir

    # -- helpers mirroring EvalContext -----------------------------------
    @property
    def run_id(self) -> str:
        return self.executor.request.run_id

    @property
    def namespace(self) -> str:
        return self.executor.request.namespace

    @property
    def pod_name(self) -> str:
        return self.executor.pod_name

    def _normalize_path(self, path: str | Path) -> str:
        return path if isinstance(path, str) else str(path)

    def write_json_artifact(self, relative_path: str | Path, payload: Mapping[str, object] | BaseModel) -> None:
        target = self.scenario_dir / Path(relative_path)
        self.executor.write_json_artifact(target, payload)

    def format(self, template: str, **extra) -> str:
        data: dict[str, str] = {"run_id": self.run_id, "namespace": self.namespace, "pod_name": self.pod_name}
        data.update(extra)
        return template.format(**data)

    def render(self, template: str) -> str:
        return self.executor.render(template)

    @property
    def last_matrix_message(self):
        return self.executor.last_matrix_message

    async def send_matrix_message(self, message: str) -> SendMatrixMessageResult:
        return await self._record_async_step(self.executor.send_matrix_message(message))

    async def wait_seconds(self, seconds: float) -> WaitSecondsResult:
        return await self._record_async_step(self.executor.wait_seconds(seconds))

    async def wait_for_matrix_response(
        self, *, sender: str | None = None, timeout_seconds: int = 60
    ) -> WaitForMatrixResponseResult:
        return await self._record_async_step(
            self.executor.wait_for_matrix_response(sender=sender, timeout_seconds=timeout_seconds)
        )

    async def expect_matrix_reply(self, equals: str, *, timeout_seconds: int = 60) -> ExpectMatrixReplyResult:
        return await self._record_async_step(self.executor.expect_matrix_reply(equals, timeout_seconds=timeout_seconds))

    async def probe_http(
        self,
        *,
        container: str | None = None,
        port: int,
        path: str = "/",
        expect_status: int = 200,
        expect_body_includes: str | None = None,
    ) -> ProbeHttpResult:
        return await self._record_async_step(
            self.executor.probe_http(
                container=container,
                port=port,
                path=path,
                expect_status=expect_status,
                expect_body_includes=expect_body_includes,
            )
        )

    async def snapshot_workspace(self, path: str | Path) -> SnapshotWorkspaceResult:
        return await self._record_async_step(
            self.executor.snapshot_workspace(self._normalize_path(path), self.scenario_dir)
        )

    async def verify_file_contents(self, path: str | Path, expected: str) -> VerifyFileContentsResult:
        return await self._record_async_step(self.executor.verify_file_contents(self._normalize_path(path), expected))

    async def verify_file_contains(
        self, path: str | Path, includes: Sequence[str], *, min_size_bytes: int | None = None
    ) -> VerifyFileContainsResult:
        return await self._record_async_step(
            self.executor.verify_file_contains(self._normalize_path(path), includes, min_size_bytes=min_size_bytes)
        )

    async def kill_process(self, *, container: str | None = None, pattern: str) -> KillProcessResult:
        return await self._record_async_step(self.executor.kill_process(container=container, pattern=pattern))

    def ok(
        self, description: str | None = None, *, status: StepStatus = StepStatus.OK, **details: object
    ) -> EvalResult:
        return EvalResult(status=status, description=description, details=dict(details))

    def record(self, result: StepResult) -> None:
        self._results.append(result)

    async def _record_async_step(self, pending: Awaitable[StepResult]) -> StepResult:
        result = await pending
        self.record(result)
        return result

    def _record_step(self, result: StepResult) -> StepResult:
        self.record(result)
        return result

    @property
    def results(self) -> list[StepResult]:
        return list(self._results)

    def fail(self, message: str) -> None:
        self.record(StepErrorResult(step_type="scenario", error=message))
        raise ScenarioExecutionError(message)

    def skip(self, reason: str) -> None:
        self.record(StepSkippedResult(step_type="scenario", reason=reason))
        raise ScenarioSkippedError(reason)


@dataclass(slots=True)
class ScenarioSuite:
    """Collection of scenario classes with optional metadata."""

    scenarios: Sequence[type[Scenario]]
    name: str | None = None
    version: str | None = None
    description: str | None = None
