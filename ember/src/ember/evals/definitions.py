from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel

from ember.integrations.gitea import GiteaRepository

from .executor import ScenarioExecutionError, ScenarioSkipped
from .kubernetes import ExecResult
from .steps import (
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
    ValidateRegexResult,
    VerifyFileContainsResult,
    VerifyFileContentsResult,
    VerifyFileTimestampsResult,
    WaitForMatrixResponseResult,
    WaitSecondsResult,
)

if TYPE_CHECKING:
    from .executor import ScenarioExecutor


class ContainerHandle:
    def __init__(self, executor: ScenarioExecutor, name: str | None) -> None:
        self._executor = executor
        self._name = name

    async def exec(self, *command: str) -> ExecResult:
        return await self._executor.run_in_container(self._name, list(command))

    async def exec_binary(self, *command: str) -> bytes:
        return await self._executor.run_in_container_binary(self._name, list(command))

    async def kill(self, pattern: str) -> KillProcessResult:
        return await self._executor.kill_process(container=self._name, pattern=pattern)


class Scenario(ABC):
    """Base class for a single evaluation scenario with built-in helpers."""

    id: str
    description: str | None = None

    def __init__(self, executor: ScenarioExecutor, scenario_dir: Path) -> None:
        self._executor = executor
        self._scenario_dir = scenario_dir
        self._results: list[StepResult] = []

    # -- lifecycle hooks -------------------------------------------------
    async def setup(self) -> None:  # pragma: no cover - default hook
        """Optional hook executed before :meth:`run`."""

    @abstractmethod
    async def run(self) -> None:
        """Execute the scenario's logic."""

    async def teardown(self, result: ScenarioResult) -> None:  # pragma: no cover - default hook
        """Optional hook executed after :meth:`run` completes."""

    # -- executor wiring -------------------------------------------------
    def _require_executor(self) -> ScenarioExecutor:
        return self._executor

    def _require_dir(self) -> Path:
        return self._scenario_dir

    # -- helpers mirroring EvalContext -----------------------------------
    @property
    def run_id(self) -> str:
        return self._require_executor().request.run_id

    @property
    def namespace(self) -> str:
        return self._require_executor().request.namespace

    @property
    def pod_name(self) -> str:
        return self._require_executor().pod_name

    @property
    def scenario_dir(self) -> Path:
        return self._require_dir()

    @property
    def expected_gitea_author(self) -> str:
        request = self._require_executor().request
        return request.gitea_username or request.ember_user_id

    def gitea(self, repo: str | GiteaRepository | None = None):

        if repo is None:
            slug = None
        elif isinstance(repo, GiteaRepository):
            slug = repo.api_path
        else:
            slug = repo
        return self._require_executor().gitea_client(slug)

    def _normalize_path(self, path: str | Path) -> str:
        return path if isinstance(path, str) else str(path)

    def write_json_artifact(self, relative_path: str | Path, payload: Mapping[str, object] | BaseModel) -> None:
        target = self._require_dir() / Path(relative_path)
        self._require_executor().write_json_artifact(target, payload)

    def format(self, template: str, **extra) -> str:
        data: dict[str, str] = {"run_id": self.run_id, "namespace": self.namespace, "pod_name": self.pod_name}
        data.update(extra)
        return template.format(**data)

    def render(self, template: str) -> str:
        return self._require_executor().render(template)

    @property
    def last_matrix_message(self):
        return self._require_executor().last_matrix_message

    async def send_matrix_message(self, message: str) -> SendMatrixMessageResult:
        result = await self._require_executor().send_matrix_message(message)
        self.record(result)
        return result

    async def wait_seconds(self, seconds: float) -> WaitSecondsResult:
        result = await self._require_executor().wait_seconds(seconds)
        self.record(result)
        return result

    async def wait_for_matrix_response(
        self, *, sender: str | None = None, timeout_seconds: int = 60
    ) -> WaitForMatrixResponseResult:
        result = await self._require_executor().wait_for_matrix_response(sender=sender, timeout_seconds=timeout_seconds)
        self.record(result)
        return result

    async def expect_matrix_reply(self, equals: str, *, timeout_seconds: int = 60) -> ExpectMatrixReplyResult:
        result = await self._require_executor().expect_matrix_reply(equals, timeout_seconds=timeout_seconds)
        self.record(result)
        return result

    def validate_last_matrix_regex(
        self, pattern: str, *, flags: str | None = None, timezone_tolerance_days: int | None = None
    ) -> ValidateRegexResult:
        result = self._require_executor().validate_last_matrix_regex(
            pattern, flags=flags, timezone_tolerance_days=timezone_tolerance_days
        )
        self.record(result)
        return result

    async def probe_http(
        self,
        *,
        container: str | None = None,
        port: int,
        path: str = "/",
        expect_status: int = 200,
        expect_body_includes: str | None = None,
    ) -> ProbeHttpResult:
        result = await self._require_executor().probe_http(
            container=container,
            port=port,
            path=path,
            expect_status=expect_status,
            expect_body_includes=expect_body_includes,
        )
        self.record(result)
        return result

    async def snapshot_workspace(self, path: str | Path) -> SnapshotWorkspaceResult:
        result = await self._require_executor().snapshot_workspace(self._normalize_path(path), self.scenario_dir)
        self.record(result)
        return result

    async def verify_file_contents(self, path: str | Path, expected: str) -> VerifyFileContentsResult:
        result = await self._require_executor().verify_file_contents(self._normalize_path(path), expected)
        self.record(result)
        return result

    async def verify_file_contains(
        self, path: str | Path, includes: Sequence[str], *, min_size_bytes: int | None = None
    ) -> VerifyFileContainsResult:
        result = await self._require_executor().verify_file_contains(
            self._normalize_path(path), includes, min_size_bytes=min_size_bytes
        )
        self.record(result)
        return result

    async def verify_file_timestamps(
        self, path: str | Path, *, minimum_entries: int = 1, order: str = "ascending"
    ) -> VerifyFileTimestampsResult:
        result = await self._require_executor().verify_file_timestamps(
            self._normalize_path(path), minimum_entries=minimum_entries, order=order
        )
        self.record(result)
        return result

    async def kill_process(self, *, container: str | None = None, pattern: str) -> KillProcessResult:
        result = await self._require_executor().kill_process(container=container, pattern=pattern)
        self.record(result)
        return result

    def container(self, name: str | None = None) -> ContainerHandle:
        return ContainerHandle(self._require_executor(), name)

    @property
    def emberd_container(self) -> ContainerHandle:
        return self.container()

    async def agent_exec(self, *command: str, container: str | None = None) -> ExecResult:
        return await self._require_executor().exec(container=container, command=list(command))

    def ok(
        self, description: str | None = None, *, status: StepStatus = StepStatus.OK, **details: object
    ) -> EvalResult:
        return EvalResult(status=status, description=description, details=dict(details))

    def record(self, result: StepResult) -> None:
        self._results.append(result)

    def results(self) -> list[StepResult]:
        return list(self._results)

    def fail(self, message: str) -> None:
        self.record(StepErrorResult(step_type="scenario", error=message))
        raise ScenarioExecutionError(message)

    def skip(self, reason: str) -> None:
        self.record(StepSkippedResult(step_type="scenario", reason=reason))
        raise ScenarioSkipped(reason)


@dataclass(slots=True)
class ScenarioSuite:
    """Collection of scenario classes with optional metadata."""

    scenarios: Sequence[type[Scenario]]
    name: str | None = None
    version: str | None = None
    description: str | None = None
