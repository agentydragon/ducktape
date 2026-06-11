from __future__ import annotations

import asyncio
import json
import shlex
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from experimental.ember_evals.kubernetes import ExecResult, NamespacedKubernetes
from experimental.ember_evals.matrix import MatrixHarness, MatrixMessage
from experimental.ember_evals.steps import (
    ExpectMatrixReplyResult,
    KillProcessResult,
    ProbeHttpResult,
    ScenarioResult,
    ScenarioStatus,
    ScenarioSuiteResult,
    SendMatrixMessageResult,
    SnapshotWorkspaceResult,
    StepErrorResult,
    StepSkippedResult,
    VerifyFileContainsResult,
    VerifyFileContentsResult,
    WaitForMatrixResponseResult,
    WaitSecondsResult,
)
from pydantic import BaseModel

from experimental.ember_evals.definitions import ScenarioSuite

if TYPE_CHECKING:
    from experimental.ember_evals.models import EvalRunRequest

DEFAULT_AGENT_CONTAINER = "emberd"


class ScenarioExecutionError(RuntimeError):
    """Raised when a scenario step fails."""


class ScenarioSkippedError(RuntimeError):
    """Raised when a scenario requests to skip itself."""


class ScenarioExecutor:
    """Coordinates scenario execution and provides helper primitives."""

    def __init__(
        self,
        *,
        request: EvalRunRequest,
        matrix: MatrixHarness,
        pod_name: str,
        artifact_dir: Path,
        kube: NamespacedKubernetes,
    ) -> None:
        self._request = request
        self._matrix = matrix
        self._pod_name = pod_name
        self._artifact_dir = artifact_dir
        self._kube = kube
        self._last_matrix_message: MatrixMessage | None = None

    # ------------------------------------------------------------------ #
    # Public properties consumed by Scenario                             #
    # ------------------------------------------------------------------ #
    @property
    def request(self) -> EvalRunRequest:
        return self._request

    @property
    def pod_name(self) -> str:
        return self._pod_name

    @property
    def last_matrix_message(self) -> MatrixMessage | None:
        return self._last_matrix_message

    def write_json_artifact(self, path: Path, payload: Mapping[str, object] | BaseModel) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = payload.model_dump() if isinstance(payload, BaseModel) else dict(payload)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def render(self, template: str) -> str:
        return template.replace("${RUN_ID}", self._request.run_id)

    # ------------------------------------------------------------------ #
    # Scenario helper primitives                                         #
    # ------------------------------------------------------------------ #
    async def send_matrix_message(self, message: str) -> SendMatrixMessageResult:
        await self._matrix.send_message(message)
        return SendMatrixMessageResult(sent=message)

    async def wait_seconds(self, seconds: float) -> WaitSecondsResult:
        start = time.monotonic()
        await asyncio.sleep(max(seconds, 0))
        actual = time.monotonic() - start
        return WaitSecondsResult(requested=seconds, actual=actual)

    async def wait_for_matrix_response(
        self, *, sender: str | None = None, timeout_seconds: int = 60
    ) -> WaitForMatrixResponseResult:
        self._last_matrix_message = await self._matrix.wait_for_message(sender=sender, timeout_seconds=timeout_seconds)
        return WaitForMatrixResponseResult(sender=self._last_matrix_message.sender, body=self._last_matrix_message.body)

    async def expect_matrix_reply(self, expected: str, *, timeout_seconds: int = 60) -> ExpectMatrixReplyResult:
        self._last_matrix_message = await self._matrix.expect_reply(expected, timeout_seconds=timeout_seconds)
        return ExpectMatrixReplyResult(expected=expected, actual=self._last_matrix_message.body)

    async def run_in_container(self, container: str | None, command: Sequence[str]) -> ExecResult:
        target = container or DEFAULT_AGENT_CONTAINER
        return await self._kube.pod_exec(self._pod_name, command, container=target)

    async def run_in_container_binary(self, container: str | None, command: Sequence[str]) -> bytes:
        target = container or DEFAULT_AGENT_CONTAINER
        return await self._kube.pod_exec_binary(self._pod_name, command, container=target)

    async def probe_http(
        self,
        *,
        container: str | None,
        port: int,
        path: str,
        expect_status: int = 200,
        expect_body_includes: str | None = None,
    ) -> ProbeHttpResult:
        normalized_path = path if path.startswith("/") else f"/{path}"
        url = f"http://127.0.0.1:{port}{normalized_path}"
        result = await self.run_in_container(container, ["curl", "-sS", "-o", "-", "-w", "\\n%{http_code}", url])
        if result.returncode != 0 or not result.stdout:
            raise ScenarioExecutionError(
                f"HTTP probe failed: {result.stderr.strip() if result.stderr else result.stdout}"
            )
        stdout = result.stdout
        body, _, status_line = stdout.rpartition("\n")
        try:
            status = int(status_line.strip())
        except ValueError as exc:
            raise ScenarioExecutionError(f"Unexpected status line from curl: {status_line!r}") from exc
        if status != expect_status:
            raise ScenarioExecutionError(f"Status {status} != expected {expect_status}")
        if expect_body_includes and expect_body_includes not in body:
            raise ScenarioExecutionError(f"Body missing expected content: {expect_body_includes!r}")
        return ProbeHttpResult(
            port=port, path=normalized_path, http_status=status, body_excerpt=body[:500] if body else None
        )

    async def snapshot_workspace(self, path: str, scenario_dir: Path) -> SnapshotWorkspaceResult:
        normalized = Path(path)
        parent = str(normalized.parent) or "/"
        name = normalized.name
        artifact_path = scenario_dir / f"{name or 'root'}-{int(time.time())}.tar.gz"
        tar_bytes = await self.run_in_container_binary(None, ["tar", "czf", "-", "-C", parent, name or "."])
        artifact_path.write_bytes(tar_bytes)
        return SnapshotWorkspaceResult(artifact=str(artifact_path.relative_to(self._artifact_dir)))

    async def verify_file_contents(self, path: str, expected: str) -> VerifyFileContentsResult:
        contents = await self._read_file(path)
        if contents != expected:
            raise ScenarioExecutionError(f"{path} contents did not match expected text")
        return VerifyFileContentsResult(path=path)

    async def verify_file_contains(
        self, path: str, includes: Iterable[str], *, min_size_bytes: int | None = None
    ) -> VerifyFileContainsResult:
        contents = await self._read_file(path)
        missing = [value for value in includes if value not in contents]
        if missing:
            raise ScenarioExecutionError(f"{path} missing expected contents: {missing}")
        if min_size_bytes is not None and len(contents.encode("utf-8")) < min_size_bytes:
            raise ScenarioExecutionError(f"{path} smaller than {min_size_bytes} bytes")
        return VerifyFileContainsResult(path=path, includes=list(includes), min_size_bytes=min_size_bytes)

    async def kill_process(self, *, container: str, pattern: str) -> KillProcessResult:
        result = await self.run_in_container(container, ["pkill", "-f", pattern])
        if result.returncode not in (0, 1):
            raise ScenarioExecutionError(f"pkill failed: {result.stderr.strip() if result.stderr else result.stdout}")
        return KillProcessResult(container=container, pattern=pattern)

    # ------------------------------------------------------------------ #
    # Scenario orchestration                                             #
    # ------------------------------------------------------------------ #
    async def run_suite(self, suite: ScenarioSuite) -> ScenarioSuiteResult:
        scenario_results: list[ScenarioResult] = []
        for index, scenario_cls in enumerate(suite.scenarios, start=1):
            scenario_dir = self._artifact_dir / "scenarios" / scenario_cls.id
            scenario_dir.mkdir(parents=True, exist_ok=True)
            scenario = scenario_cls(self, scenario_dir)

            print(
                f"[ember-eval][{self._request.run_id}] ⇢ Scenario {index}: {scenario.id}"
                + (f" - {scenario.description}" if scenario.description else "")
            )

            status = ScenarioStatus.PASSED
            error: str | None = None

            try:
                await scenario.execute()
            except ScenarioSkippedError as exc:
                status = ScenarioStatus.SKIPPED
                error = str(exc)
                scenario.record(StepSkippedResult(step_type="scenario", reason=error or "scenario skipped"))
            except ScenarioExecutionError as exc:
                status = ScenarioStatus.FAILED
                error = str(exc)
                scenario.record(StepErrorResult(step_type="scenario", error=error))
            except Exception as exc:  # pragma: no cover - defensive
                status = ScenarioStatus.FAILED
                error = f"Unexpected error: {exc}"
                scenario.record(StepErrorResult(step_type="scenario", error=error))
                raise
            finally:
                result = ScenarioResult(
                    id=scenario.id, description=scenario.description, status=status, steps=scenario.results, error=error
                )
                try:
                    await scenario.teardown(result)
                except Exception as exc:  # pragma: no cover - teardown resilience
                    print(f"[ember-eval][{self._request.run_id}] teardown for {scenario.id} raised: {exc}", flush=True)
                scenario_results.append(result)
                print(f"[ember-eval][{self._request.run_id}] ⇠ Scenario {index} completed with status {status.value}")

        return ScenarioSuiteResult(scenarios=scenario_results)

    # ------------------------------------------------------------------ #
    # Internal utilities                                                 #
    # ------------------------------------------------------------------ #
    def _require_last_message(self) -> MatrixMessage:
        if self._last_matrix_message is None:
            raise ScenarioExecutionError("No Matrix message recorded yet")
        return self._last_matrix_message

    async def _read_file(self, path: str) -> str:
        command = ["/bin/sh", "-c", f"cat {shlex.quote(path)}"]
        result = await self.run_in_container("emberd", command)
        if result.returncode != 0:
            raise ScenarioExecutionError(
                f"Failed to read file {path}: {result.stderr.strip() if result.stderr else result.stdout}"
            )
        return result.stdout


__all__ = ["ScenarioExecutionError", "ScenarioExecutor", "ScenarioSkippedError"]
