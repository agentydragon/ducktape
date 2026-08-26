"""Tests for kubectl-passthrough-mcp policy mapping and approval suppression."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import pytest_bazel

from haku.console.conftest import console_settings, write_config
from haku.console.kubectl_passthrough_policy import map_kubectl_passthrough_request
from haku.console.kubernetes_authorization import AuthorizationResponse, KubernetesAuthorizationSource
from haku.console.tool_call_actor import AgentActor
from haku.console.tool_call_service import ToolCallApplicationService
from haku.console.tool_calls import SubmitToolCallRequest, ToolCallStatus


def test_map_pods_list() -> None:
    reqs = map_kubectl_passthrough_request("pods_list", {"namespace": "default"})
    assert reqs is not None
    assert len(reqs) == 1
    assert reqs[0].attributes.verb == "list"
    assert reqs[0].attributes.resource == "pods"
    assert reqs[0].attributes.namespace == "default"


def test_map_pods_log() -> None:
    reqs = map_kubectl_passthrough_request("pods_log", {"name": "my-pod", "namespace": "demo"})
    assert reqs is not None
    assert len(reqs) == 1
    assert reqs[0].attributes.verb == "get"
    assert reqs[0].attributes.resource == "pods"
    assert reqs[0].attributes.subresource == "log"
    assert reqs[0].attributes.name == "my-pod"
    assert reqs[0].attributes.namespace == "demo"


def test_map_pods_exec() -> None:
    reqs = map_kubectl_passthrough_request("pods_exec", {"name": "my-pod", "namespace": "default"})
    assert reqs is not None
    assert len(reqs) == 2
    assert reqs[0].attributes.verb == "create"
    assert reqs[0].attributes.subresource == "exec"
    assert reqs[1].attributes.verb == "get"
    assert reqs[1].attributes.resource == "pods"


def test_map_unknown_tool() -> None:
    assert map_kubectl_passthrough_request("unknown_tool", {}) is None


@pytest.mark.asyncio
async def test_kubectl_passthrough_suppression_when_fully_covered(tmp_path: Path) -> None:
    repo = AsyncMock()
    submitted_record = AsyncMock()
    submitted_record.tool_call_id = "tc_123"
    submitted_record.status = ToolCallStatus.DENIED
    repo.submit.return_value = submitted_record

    authorization = AsyncMock()
    authorization.evaluate.return_value = AuthorizationResponse(
        allowed=True, reason="covered by SAR", source=KubernetesAuthorizationSource.SAR, decision_id="sar:1"
    )

    config_file = write_config(
        tmp_path / "config.yaml",
        {
            "auto_approval_policies": [
                {"id": "k8s-passthrough", "type": "kubernetes_passthrough", "server": "kubectl-passthrough-mcp"}
            ],
            "access_profiles": [{"id": "public-coder", "auto_approval_policy": "k8s-passthrough"}],
            "default_access_profile_id": "public-coder",
            "mcp": {
                "servers": [
                    {
                        "id": "kubectl-passthrough-mcp",
                        "backend": {
                            "kind": "remote_mcp",
                            "url": "https://kubectl-passthrough.test/mcp",
                            "auth": {"kind": "none"},
                        },
                    }
                ]
            },
        },
    )
    service = ToolCallApplicationService(
        settings=console_settings("postgresql://...", config_file=config_file),
        repository=repo,
        invalidation_publisher=AsyncMock(),
        executor=AsyncMock(),
        oauth_store=AsyncMock(),
        in_process_servers={},
        provider_store=AsyncMock(),
        authentik_token_store=AsyncMock(),
        approval_notifier=AsyncMock(),
        gmail_client_provider=AsyncMock(return_value=None),
        kubernetes_authorization=authorization,
    )

    agent = AgentActor(
        agent_id=UUID("10000000-0000-4000-8000-000000000001"),
        operator_id=UUID("20000000-0000-4000-8000-000000000002"),
        binding_id=UUID("30000000-0000-4000-8000-000000000003"),
        access_profile_id="public-coder",
    )

    req = SubmitToolCallRequest(
        server_id="kubectl-passthrough-mcp", tool_name="pods_list", arguments={"namespace": "default"}, wait_for_ms=0
    )

    record = await service.submit_and_wait(req=req, actor=agent)
    assert record.status == ToolCallStatus.DENIED
    repo.submit.assert_awaited_once()
    kwargs = repo.submit.call_args.kwargs
    assert "covered by direct agent access" in kwargs["auto_approval_evaluation"]
    assert "Use your direct Haku Kubernetes proxy" in kwargs["auto_denial_reason"]


@pytest.mark.asyncio
async def test_kubectl_passthrough_falls_through_when_denied(tmp_path: Path) -> None:
    repo = AsyncMock()
    submitted_record = AsyncMock()
    submitted_record.tool_call_id = "tc_123"
    submitted_record.status = ToolCallStatus.PENDING_APPROVAL
    repo.submit.return_value = submitted_record
    repo.get.return_value = submitted_record

    authorization = AsyncMock()
    authorization.evaluate.return_value = AuthorizationResponse(
        allowed=False, reason="not permitted", source=KubernetesAuthorizationSource.SAR, decision_id="sar:1"
    )

    config_file = write_config(
        tmp_path / "config.yaml",
        {
            "auto_approval_policies": [
                {"id": "k8s-passthrough", "type": "kubernetes_passthrough", "server": "kubectl-passthrough-mcp"}
            ],
            "access_profiles": [{"id": "public-coder", "auto_approval_policy": "k8s-passthrough"}],
            "default_access_profile_id": "public-coder",
            "mcp": {
                "servers": [
                    {
                        "id": "kubectl-passthrough-mcp",
                        "backend": {
                            "kind": "remote_mcp",
                            "url": "https://kubectl-passthrough.test/mcp",
                            "auth": {"kind": "none"},
                        },
                    }
                ]
            },
        },
    )
    service = ToolCallApplicationService(
        settings=console_settings("postgresql://...", config_file=config_file),
        repository=repo,
        invalidation_publisher=AsyncMock(),
        executor=AsyncMock(),
        oauth_store=AsyncMock(),
        in_process_servers={},
        provider_store=AsyncMock(),
        authentik_token_store=AsyncMock(),
        approval_notifier=AsyncMock(),
        gmail_client_provider=AsyncMock(return_value=None),
        kubernetes_authorization=authorization,
    )

    agent = AgentActor(
        agent_id=UUID("10000000-0000-4000-8000-000000000001"),
        operator_id=UUID("20000000-0000-4000-8000-000000000002"),
        binding_id=UUID("30000000-0000-4000-8000-000000000003"),
        access_profile_id="public-coder",
    )

    req = SubmitToolCallRequest(
        server_id="kubectl-passthrough-mcp", tool_name="pods_list", arguments={"namespace": "default"}, wait_for_ms=0
    )

    record = await service.submit_and_wait(req=req, actor=agent)
    assert record.status == ToolCallStatus.PENDING_APPROVAL
    repo.submit.assert_awaited_once()
    kwargs = repo.submit.call_args.kwargs
    assert kwargs.get("auto_denial_reason") is None


if __name__ == "__main__":
    pytest_bazel.main()
