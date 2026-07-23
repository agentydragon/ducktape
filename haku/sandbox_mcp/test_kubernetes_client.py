"""Lifecycle tests for the external Agent Sandbox CRD integration."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_bazel
from fastmcp.exceptions import ToolError
from kubernetes_asyncio.client import ApiException

from haku.sandbox_mcp.config import EnvironmentConfig
from haku.sandbox_mcp.kubernetes_client import (
    API_VERSION,
    BOOTSTRAP_STARTED_AT_ANNOTATION,
    BOOTSTRAP_STATE_ANNOTATION,
    CLAIM_GROUP,
    CLAIMS_PLURAL,
    CONFIG_HASH_ANNOTATION,
    MANAGED_BY_LABEL,
    MANAGED_BY_VALUE,
    POD_NAME_ANNOTATION,
    SANDBOX_GROUP,
    SANDBOXES_PLURAL,
    CommandResult,
    KubernetesSandboxClient,
)
from mcp_infra.exec.models import Exited

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)


def _environment() -> EnvironmentConfig:
    return EnvironmentConfig.model_validate(
        {
            "sandbox": {
                "namespace": "agent-workspaces",
                "warm_pool": "haku",
                "container": "workspace",
                "default_cwd": "/workspace/haku-state",
                "initial_ttl_seconds": 28_800,
                "exec_ttl_extension_seconds": 7_200,
                "provisioning_timeout_seconds": 10,
                "max_exec_timeout_seconds": 300,
                "max_output_bytes": 100_000,
            },
            "bootstrap": {"cwd": "/workspace", "timeout_seconds": 30, "script": "echo bootstrap"},
        }
    )


def _claim(
    environment: EnvironmentConfig,
    *,
    deadline: datetime,
    bootstrap_state: str = "succeeded",
    contract_hash: str | None = None,
) -> dict:
    return {
        "metadata": {
            "name": "task-one",
            "resourceVersion": "7",
            "creationTimestamp": NOW.isoformat(),
            "labels": {MANAGED_BY_LABEL: MANAGED_BY_VALUE},
            "annotations": {
                CONFIG_HASH_ANNOTATION: contract_hash or environment.contract_hash,
                BOOTSTRAP_STATE_ANNOTATION: bootstrap_state,
            },
        },
        "spec": {
            "warmPoolRef": {"name": "haku"},
            "lifecycle": {"shutdownPolicy": "Delete", "shutdownTime": deadline.isoformat()},
        },
        "status": {
            "conditions": [{"type": "Ready", "status": "True", "reason": "Ready"}],
            "sandbox": {"name": "haku-abcde"},
        },
    }


def _sandbox() -> dict:
    return {
        "metadata": {"name": "haku-abcde", "annotations": {POD_NAME_ANNOTATION: "pod-abcde"}},
        "status": {"conditions": [{"type": "Ready", "status": "True", "reason": "Ready"}]},
    }


def _pod(*, ready: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        status=SimpleNamespace(phase="Running", container_statuses=[SimpleNamespace(name="workspace", ready=ready)])
    )


def _runner_result() -> CommandResult:
    return CommandResult(exit=Exited(exit_code=0), stdout="ok", stderr="", duration_seconds=0.1)


def _client(environment: EnvironmentConfig, custom: Mock, core: Mock, runner: Mock) -> KubernetesSandboxClient:
    return KubernetesSandboxClient(
        environment, api_client=Mock(), custom_objects=custom, core_v1=core, exec_runner=runner, now=lambda: NOW
    )


def _route_get(claim: dict):
    async def get(group: str, version: str, namespace: str, plural: str, name: str):
        assert version == API_VERSION
        assert namespace == "agent-workspaces"
        if (group, plural, name) == (CLAIM_GROUP, CLAIMS_PLURAL, "task-one"):
            return claim
        if (group, plural, name) == (SANDBOX_GROUP, SANDBOXES_PLURAL, "haku-abcde"):
            return _sandbox()
        raise AssertionError((group, plural, name))

    return get


async def test_provision_creates_named_delete_claim_and_adopts_ready_result() -> None:
    environment = _environment()
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
    assert body["spec"]["warmPoolRef"] == {"name": "haku"}
    assert body["spec"]["lifecycle"]["shutdownPolicy"] == "Delete"
    assert body["metadata"]["annotations"][CONFIG_HASH_ANNOTATION] == environment.contract_hash


async def test_provision_adopts_matching_claim_after_create_conflict() -> None:
    environment = _environment()
    claim = _claim(environment, deadline=NOW + timedelta(hours=8))
    custom = Mock()
    custom.create_namespaced_custom_object = AsyncMock(side_effect=ApiException(status=409))
    custom.get_namespaced_custom_object = AsyncMock(side_effect=_route_get(claim))
    core = Mock()
    core.read_namespaced_pod = AsyncMock(return_value=_pod())

    result = await _client(environment, custom, core, Mock()).provision("task-one")

    assert result.state == "ready"


async def test_changed_configuration_is_visible_but_cannot_execute() -> None:
    environment = _environment()
    claim = _claim(environment, deadline=NOW + timedelta(hours=8), contract_hash="old")
    custom = Mock()
    custom.get_namespaced_custom_object = AsyncMock(side_effect=_route_get(claim))
    core = Mock()
    core.read_namespaced_pod = AsyncMock(return_value=_pod())
    client = _client(environment, custom, core, Mock())

    assert (await client.info("task-one")).state == "stale_config"
    with pytest.raises(ToolError, match="different server configuration"):
        await client.execute(name="task-one", script="true", cwd=None, timeout_seconds=1, max_output_bytes=100)


async def test_exec_renews_near_deadline_before_running() -> None:
    environment = _environment()
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
        cwd="/workspace/haku-state",
        max_output_bytes=1000,
        timeout_seconds=30,
    )
    assert result.expires_at == NOW + timedelta(hours=2)


async def test_exec_does_not_shorten_later_deadline() -> None:
    environment = _environment()
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


async def test_exec_is_available_for_bootstrap_diagnostics() -> None:
    environment = _environment()
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


async def test_interrupted_bootstrap_becomes_retryable() -> None:
    environment = _environment()
    claim = _claim(environment, deadline=NOW + timedelta(hours=6), bootstrap_state="running")
    claim["metadata"]["annotations"][BOOTSTRAP_STARTED_AT_ANNOTATION] = (NOW - timedelta(minutes=1)).isoformat()
    custom = Mock()
    custom.get_namespaced_custom_object = AsyncMock(side_effect=_route_get(claim))
    core = Mock()
    core.read_namespaced_pod = AsyncMock(return_value=_pod())

    info = await _client(environment, custom, core, Mock()).info("task-one")

    assert info.state == "bootstrap_failed"
    assert info.bootstrap_state == "failed"


async def test_renewal_failure_prevents_exec() -> None:
    environment = _environment()
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


async def test_dispose_is_idempotent() -> None:
    environment = _environment()
    custom = Mock()
    custom.get_namespaced_custom_object = AsyncMock(side_effect=ApiException(status=404))

    result = await _client(environment, custom, Mock(), Mock()).dispose("task-one")

    assert not result.deleted


if __name__ == "__main__":
    pytest_bazel.main()
