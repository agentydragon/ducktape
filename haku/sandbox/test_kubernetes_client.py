"""Lifecycle tests for the external Agent Sandbox CRD integration."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_bazel
from fastmcp.exceptions import ToolError
from kubernetes_asyncio.client import ApiException

from haku.sandbox.config import SandboxEnvironmentConfig
from haku.sandbox.kubernetes_client import (
    API_VERSION,
    BOOTSTRAP_HASH_ANNOTATION,
    BOOTSTRAP_STARTED_AT_ANNOTATION,
    BOOTSTRAP_STATE_ANNOTATION,
    CLAIM_GROUP,
    CLAIMS_PLURAL,
    CONTAINER_ANNOTATION,
    DEFAULT_CWD_ANNOTATION,
    MANAGED_BY_LABEL,
    MANAGED_BY_VALUE,
    POD_NAME_ANNOTATION,
    SANDBOX_GROUP,
    SANDBOXES_PLURAL,
    WARM_POOL_ANNOTATION,
    CommandResult,
    KubernetesSandboxClient,
    SandboxClaimClient,
    _exec_handshake_error,
)
from mcp_infra.exec.models import Exited

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
SANDBOX: dict[str, Any] = {
    "metadata": {"name": "test-sandbox-abcde", "annotations": {POD_NAME_ANNOTATION: "pod-abcde"}},
    "status": {"conditions": [{"type": "Ready", "status": "True", "reason": "Ready"}]},
}


@pytest.fixture
def environment() -> SandboxEnvironmentConfig:
    return SandboxEnvironmentConfig.model_validate(
        {
            "sandbox": {
                "namespace": "agent-workspaces",
                "warm_pool": "test-pool",
                "container": "workspace",
                "default_cwd": "/test/workspace/test-state",
                "initial_ttl_seconds": 28_800,
                "exec_ttl_extension_seconds": 7_200,
                "provisioning_timeout_seconds": 10,
                "max_exec_timeout_seconds": 300,
                "max_output_bytes": 100_000,
            },
            "bootstrap": {"cwd": "/test/workspace", "timeout_seconds": 30, "script": "echo bootstrap"},
        }
    )


def _claim(
    environment: SandboxEnvironmentConfig,
    *,
    deadline: datetime,
    bootstrap_state: str = "succeeded",
    annotations: dict[str, str] | None = None,
) -> dict:
    recorded = {
        WARM_POOL_ANNOTATION: environment.sandbox.warm_pool,
        CONTAINER_ANNOTATION: environment.sandbox.container,
        DEFAULT_CWD_ANNOTATION: environment.sandbox.default_cwd,
        BOOTSTRAP_HASH_ANNOTATION: environment.bootstrap.script_digest,
        BOOTSTRAP_STATE_ANNOTATION: bootstrap_state,
    }
    return {
        "metadata": {
            "name": "task-one",
            "resourceVersion": "7",
            "creationTimestamp": NOW.isoformat(),
            "labels": {MANAGED_BY_LABEL: MANAGED_BY_VALUE},
            "annotations": recorded | (annotations or {}),
        },
        "spec": {
            "warmPoolRef": {"name": "test-pool"},
            "lifecycle": {"shutdownPolicy": "Delete", "shutdownTime": deadline.isoformat()},
        },
        "status": {
            "conditions": [{"type": "Ready", "status": "True", "reason": "Ready"}],
            "sandbox": {"name": "test-sandbox-abcde"},
        },
    }


def _pod(*, ready: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        status=SimpleNamespace(phase="Running", container_statuses=[SimpleNamespace(name="workspace", ready=ready)])
    )


def _runner_result() -> CommandResult:
    return CommandResult(exit=Exited(exit_code=0), stdout="ok", stderr="", duration_seconds=0.1)


def _client(environment: SandboxEnvironmentConfig, custom: Mock, core: Mock, runner: Mock) -> KubernetesSandboxClient:
    claims = SandboxClaimClient(custom, core, environment.sandbox.namespace)
    return KubernetesSandboxClient(
        environment,
        api_client=Mock(),
        claims=claims,
        exec_runner=runner,
        now=lambda: NOW,
    )


def _route_get(claim: dict):
    async def get(group: str, version: str, namespace: str, plural: str, name: str):
        assert version == API_VERSION
        assert namespace == "agent-workspaces"
        if (group, plural, name) == (CLAIM_GROUP, CLAIMS_PLURAL, "task-one"):
            return claim
        if (group, plural, name) == (SANDBOX_GROUP, SANDBOXES_PLURAL, "test-sandbox-abcde"):
            return SANDBOX
        raise AssertionError((group, plural, name))

    return get


async def test_provision_creates_named_delete_claim_and_adopts_ready_result(
    environment: SandboxEnvironmentConfig,
) -> None:
    claim = _claim(environment, deadline=NOW + timedelta(hours=8))
    custom = Mock()
    custom.create_namespaced_custom_object = AsyncMock(return_value=claim)
    custom.get_namespaced_custom_object = AsyncMock(side_effect=_route_get(claim))
    core = Mock()
    core.read_namespaced_pod = AsyncMock(return_value=_pod())

    result = await _client(environment, custom, core, Mock()).provision("task-one")

    assert result.state == "ready"
    body = custom.create_namespaced_custom_object.await_args.args[4]
    assert body["metadata"]["name"] == "task-one"
    assert body["spec"]["warmPoolRef"] == {"name": "test-pool"}
    assert body["spec"]["lifecycle"]["shutdownPolicy"] == "Delete"
    recorded = body["metadata"]["annotations"]
    assert recorded[WARM_POOL_ANNOTATION] == environment.sandbox.warm_pool
    assert recorded[CONTAINER_ANNOTATION] == environment.sandbox.container
    assert recorded[DEFAULT_CWD_ANNOTATION] == environment.sandbox.default_cwd
    assert "env" not in body["spec"]
    # Per-call and lifecycle budgets describe no property of the pod, so nothing records them.
    assert not [key for key in recorded if "ttl" in key or "timeout" in key or "bytes" in key]


async def test_provision_passes_internal_env_into_new_claim(environment: SandboxEnvironmentConfig) -> None:
    claim = _claim(environment, deadline=NOW + timedelta(hours=8))
    custom = Mock()
    custom.create_namespaced_custom_object = AsyncMock(return_value=claim)

    await _client(environment, custom, Mock(), Mock())._create_or_adopt_claim(
        "task-one", env={"HAKU_CONSOLE_TOKEN": "minted-token", "EXTRA": "value"}
    )

    body = custom.create_namespaced_custom_object.await_args.args[4]
    assert body["spec"]["env"] == [
        {"name": "HAKU_CONSOLE_TOKEN", "value": "minted-token"},
        {"name": "EXTRA", "value": "value"},
    ]


async def test_provision_adopts_matching_claim_after_create_conflict(environment: SandboxEnvironmentConfig) -> None:
    claim = _claim(environment, deadline=NOW + timedelta(hours=8))
    custom = Mock()
    custom.create_namespaced_custom_object = AsyncMock(side_effect=ApiException(status=409))
    custom.get_namespaced_custom_object = AsyncMock(side_effect=_route_get(claim))
    core = Mock()
    core.read_namespaced_pod = AsyncMock(return_value=_pod())

    result = await _client(environment, custom, core, Mock()).provision("task-one")

    assert result.state == "ready"


async def test_diverged_bootstrap_warns_and_stays_usable(environment: SandboxEnvironmentConfig) -> None:
    claim = _claim(
        environment, deadline=NOW + timedelta(hours=8), annotations={BOOTSTRAP_HASH_ANNOTATION: "0123456789abcdef"}
    )
    custom = Mock()
    custom.get_namespaced_custom_object = AsyncMock(side_effect=_route_get(claim))
    core = Mock()
    core.read_namespaced_pod = AsyncMock(return_value=_pod())
    runner = Mock()
    runner.run = AsyncMock(return_value=_runner_result())
    client = _client(environment, custom, core, runner)

    info = await client.info("task-one")
    result = await client.execute(name="task-one", script="true", cwd=None, timeout_seconds=1, max_output_bytes=100)

    assert info.state == "ready"
    assert [warning.kind for warning in info.warnings] == ["bootstrap_script_changed"]
    assert "0123456789abcdef" in info.warnings[0].detail
    assert environment.bootstrap.script_digest in info.warnings[0].detail
    assert result.stdout == "ok"


async def test_pod_describing_fields_each_warn_by_kind(environment: SandboxEnvironmentConfig) -> None:
    claim = _claim(
        environment,
        deadline=NOW + timedelta(hours=8),
        annotations={
            WARM_POOL_ANNOTATION: "retired-pool",
            CONTAINER_ANNOTATION: "old-workspace",
            DEFAULT_CWD_ANNOTATION: "/test/workspace/old",
        },
    )
    custom = Mock()
    custom.get_namespaced_custom_object = AsyncMock(side_effect=_route_get(claim))
    core = Mock()
    core.read_namespaced_pod = AsyncMock(return_value=_pod())

    info = await _client(environment, custom, core, Mock()).info("task-one")

    assert info.state == "ready"
    assert {warning.kind for warning in info.warnings} == {
        "warm_pool_changed",
        "container_changed",
        "default_cwd_changed",
    }


async def test_claim_without_provenance_is_unknown_not_diverged(environment: SandboxEnvironmentConfig) -> None:
    claim = _claim(environment, deadline=NOW + timedelta(hours=8))
    claim["metadata"]["annotations"] = {BOOTSTRAP_STATE_ANNOTATION: "succeeded"}
    custom = Mock()
    custom.get_namespaced_custom_object = AsyncMock(side_effect=_route_get(claim))
    core = Mock()
    core.read_namespaced_pod = AsyncMock(return_value=_pod())

    info = await _client(environment, custom, core, Mock()).info("task-one")

    assert info.state == "ready"
    assert [warning.kind for warning in info.warnings] == ["provenance_unknown"]


async def test_unknown_annotations_are_ignored(environment: SandboxEnvironmentConfig) -> None:
    """A claim written by a newer Console must stay readable through a rolling deploy."""

    claim = _claim(
        environment, deadline=NOW + timedelta(hours=8), annotations={"haku.allegedly.works/sandbox-future": "x"}
    )
    custom = Mock()
    custom.get_namespaced_custom_object = AsyncMock(side_effect=_route_get(claim))
    core = Mock()
    core.read_namespaced_pod = AsyncMock(return_value=_pod())

    info = await _client(environment, custom, core, Mock()).info("task-one")

    assert info.state == "ready"
    assert info.warnings == []


async def test_unbootstrapped_claim_does_not_warn_about_the_script(environment: SandboxEnvironmentConfig) -> None:
    """Nothing has run yet, so the current script is what the claim will get."""

    claim = _claim(environment, deadline=NOW + timedelta(hours=8), bootstrap_state="pending")
    del claim["metadata"]["annotations"][BOOTSTRAP_HASH_ANNOTATION]
    custom = Mock()
    custom.get_namespaced_custom_object = AsyncMock(side_effect=_route_get(claim))
    core = Mock()
    core.read_namespaced_pod = AsyncMock(return_value=_pod())

    info = await _client(environment, custom, core, Mock()).info("task-one")

    assert info.warnings == []


async def test_bootstrap_records_the_script_it_ran(environment: SandboxEnvironmentConfig) -> None:
    claim = _claim(environment, deadline=NOW + timedelta(hours=8), bootstrap_state="pending")
    del claim["metadata"]["annotations"][BOOTSTRAP_HASH_ANNOTATION]
    custom = Mock()
    custom.create_namespaced_custom_object = AsyncMock(return_value=claim)
    custom.get_namespaced_custom_object = AsyncMock(side_effect=_route_get(claim))
    custom.patch_namespaced_custom_object = AsyncMock()
    core = Mock()
    core.read_namespaced_pod = AsyncMock(return_value=_pod())
    runner = Mock()
    runner.run = AsyncMock(return_value=_runner_result())

    await _client(environment, custom, core, runner).provision("task-one")

    started = custom.patch_namespaced_custom_object.await_args_list[0].args[5]["metadata"]["annotations"]
    assert started[BOOTSTRAP_STATE_ANNOTATION] == "running"
    assert started[BOOTSTRAP_HASH_ANNOTATION] == environment.bootstrap.script_digest


async def test_exec_renews_near_deadline_before_running(environment: SandboxEnvironmentConfig) -> None:
    claim = _claim(environment, deadline=NOW + timedelta(minutes=5))
    custom = Mock()
    custom.get_namespaced_custom_object = AsyncMock(side_effect=_route_get(claim))
    custom.patch_namespaced_custom_object = AsyncMock(return_value=claim)
    core = Mock()
    core.read_namespaced_pod = AsyncMock(return_value=_pod())
    runner = Mock()
    runner.run = AsyncMock(return_value=_runner_result())

    result = await _client(environment, custom, core, runner).execute(
        name="task-one", script="echo ok", cwd=None, timeout_seconds=30, max_output_bytes=1000
    )

    patch = custom.patch_namespaced_custom_object.await_args.args[5]
    assert patch[0] == {"op": "test", "path": "/metadata/resourceVersion", "value": "7"}
    assert patch[1]["value"] == "2026-07-22T14:00:00Z"
    runner.run.assert_awaited_once_with(
        pod_name="pod-abcde",
        namespace="agent-workspaces",
        container="workspace",
        script="echo ok",
        cwd="/test/workspace/test-state",
        max_output_bytes=1000,
        timeout_seconds=30,
    )
    assert result.expires_at == NOW + timedelta(hours=2)


async def test_exec_does_not_shorten_later_deadline(environment: SandboxEnvironmentConfig) -> None:
    claim = _claim(environment, deadline=NOW + timedelta(hours=6))
    custom = Mock()
    custom.get_namespaced_custom_object = AsyncMock(side_effect=_route_get(claim))
    custom.patch_namespaced_custom_object = AsyncMock()
    core = Mock()
    core.read_namespaced_pod = AsyncMock(return_value=_pod())
    runner = Mock()
    runner.run = AsyncMock(return_value=_runner_result())

    result = await _client(environment, custom, core, runner).execute(
        name="task-one", script="true", cwd=None, timeout_seconds=1, max_output_bytes=100
    )

    custom.patch_namespaced_custom_object.assert_not_awaited()
    assert result.expires_at == NOW + timedelta(hours=6)


async def test_exec_is_available_for_bootstrap_diagnostics(environment: SandboxEnvironmentConfig) -> None:
    claim = _claim(environment, deadline=NOW + timedelta(hours=6), bootstrap_state="failed")
    custom = Mock()
    custom.get_namespaced_custom_object = AsyncMock(side_effect=_route_get(claim))
    custom.patch_namespaced_custom_object = AsyncMock()
    core = Mock()
    core.read_namespaced_pod = AsyncMock(return_value=_pod())
    runner = Mock()
    runner.run = AsyncMock(return_value=_runner_result())

    result = await _client(environment, custom, core, runner).execute(
        name="task-one", script="git status", cwd=None, timeout_seconds=1, max_output_bytes=100
    )

    assert result.stdout == "ok"
    runner.run.assert_awaited_once()


async def test_interrupted_bootstrap_becomes_failed(environment: SandboxEnvironmentConfig) -> None:
    claim = _claim(environment, deadline=NOW + timedelta(hours=6), bootstrap_state="running")
    claim["metadata"]["annotations"][BOOTSTRAP_STARTED_AT_ANNOTATION] = (NOW - timedelta(minutes=1)).isoformat()
    custom = Mock()
    custom.get_namespaced_custom_object = AsyncMock(side_effect=_route_get(claim))
    core = Mock()
    core.read_namespaced_pod = AsyncMock(return_value=_pod())

    info = await _client(environment, custom, core, Mock()).info("task-one")

    assert info.state == "ready"
    assert info.bootstrap_state == "failed"


async def test_provision_does_not_retry_failed_bootstrap(environment: SandboxEnvironmentConfig) -> None:
    claim = _claim(environment, deadline=NOW + timedelta(hours=6), bootstrap_state="failed")
    custom = Mock()
    custom.create_namespaced_custom_object = AsyncMock(side_effect=ApiException(status=409))
    custom.get_namespaced_custom_object = AsyncMock(side_effect=_route_get(claim))
    core = Mock()
    core.read_namespaced_pod = AsyncMock(return_value=_pod())
    runner = Mock()
    runner.run = AsyncMock()

    info = await _client(environment, custom, core, runner).provision("task-one")

    assert info.state == "ready"
    assert info.bootstrap_state == "failed"
    runner.run.assert_not_awaited()


async def test_renewal_failure_prevents_exec(environment: SandboxEnvironmentConfig) -> None:
    claim = _claim(environment, deadline=NOW + timedelta(minutes=5))
    custom = Mock()
    custom.get_namespaced_custom_object = AsyncMock(side_effect=_route_get(claim))
    custom.patch_namespaced_custom_object = AsyncMock(side_effect=ApiException(status=403, reason="Forbidden"))
    runner = Mock()
    runner.run = AsyncMock()

    with pytest.raises(ToolError, match="command was not executed"):
        await _client(environment, custom, Mock(), runner).execute(
            name="task-one", script="true", cwd=None, timeout_seconds=1, max_output_bytes=100
        )

    runner.run.assert_not_awaited()


async def test_dispose_is_idempotent(environment: SandboxEnvironmentConfig) -> None:
    custom = Mock()
    custom.get_namespaced_custom_object = AsyncMock(side_effect=ApiException(status=404))

    result = await _client(environment, custom, Mock(), Mock()).dispose("task-one")

    assert not result.deleted


@pytest.mark.parametrize(
    ("status", "needle"),
    [
        # 403 is the real bug: the async client execs via HTTP GET, so it needs `get pods/exec`.
        (403, "get pods/exec"),
        (401, "token was rejected"),
        (503, "container is ready"),
    ],
)
def test_exec_handshake_error_names_likely_cause(status: int, needle: str) -> None:
    message = _exec_handshake_error(status, "Forbidden")
    assert f"HTTP {status}" in message
    assert needle in message


if __name__ == "__main__":
    pytest_bazel.main()
