"""Tests for kubectl-passthrough-mcp policy mapping and approval suppression."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import pytest_bazel

from haku.console.conftest import console_settings, write_config
from haku.console.grants.kubernetes.authorization import AuthorizationResponse, KubernetesAuthorizationSource
from haku.console.grants.kubernetes.kubectl_passthrough_policy import map_kubectl_passthrough_request
from haku.console.mcp.tool_call_service import ToolCallApplicationService
from haku.console.tool_call_actor import AgentActor
from haku.console.tool_calls import SubmitToolCallRequest, ToolCallStatus

_SERVER_ID = "kubectl-passthrough-mcp"

_AGENT = AgentActor(
    agent_id=UUID("10000000-0000-4000-8000-000000000001"),
    operator_id=UUID("20000000-0000-4000-8000-000000000002"),
    binding_id=UUID("30000000-0000-4000-8000-000000000003"),
    access_profile_id="public-coder",
)

_PODS_LIST = SubmitToolCallRequest(
    server_id=_SERVER_ID, tool_name="pods_list", arguments={"namespace": "default"}, wait_for_ms=0
)


def _repository(status: ToolCallStatus) -> AsyncMock:
    """A ledger whose submitted record comes back in *status*."""
    repository = AsyncMock()
    record = AsyncMock()
    record.tool_call_id = "tc_123"
    record.status = status
    repository.submit.return_value = record
    repository.get.return_value = record
    return repository


def _authorization(*, allowed: bool, reason: str) -> AsyncMock:
    authorization = AsyncMock()
    authorization.evaluate.return_value = AuthorizationResponse(
        allowed=allowed, reason=reason, source=KubernetesAuthorizationSource.SAR, decision_id="sar:1"
    )
    return authorization


def _service(tmp_path: Path, *, repository: AsyncMock, authorization: AsyncMock) -> ToolCallApplicationService:
    """The service under test: one passthrough server governed by a kubernetes_passthrough policy."""
    config_file = write_config(
        tmp_path / "config.yaml",
        {
            "auto_approval_policies": [
                {"id": "k8s-passthrough", "type": "kubernetes_passthrough", "server": _SERVER_ID}
            ],
            "access_profiles": [{"id": "public-coder", "auto_approval_policy": "k8s-passthrough"}],
            "default_access_profile_id": "public-coder",
            "mcp": {
                "servers": [
                    {
                        "id": _SERVER_ID,
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
    return ToolCallApplicationService(
        settings=console_settings("postgresql://...", config_file=config_file),
        repository=repository,
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


def test_map_pods_list() -> None:
    reqs = map_kubectl_passthrough_request("pods_list", {"namespace": "default"})
    assert reqs is not None
    assert len(reqs) == 1
    assert reqs[0].attributes.verb == "list"
    assert reqs[0].attributes.resource == "pods"
    assert reqs[0].attributes.namespace == "default"


def test_map_pods_list_in_namespace() -> None:
    reqs = map_kubectl_passthrough_request("pods_list_in_namespace", {"namespace": "demo"})
    assert reqs is not None
    assert len(reqs) == 1
    assert reqs[0].attributes.verb == "list"
    assert reqs[0].attributes.resource == "pods"
    assert reqs[0].attributes.namespace == "demo"


def test_map_pods_list_in_namespace_without_namespace() -> None:
    assert map_kubectl_passthrough_request("pods_list_in_namespace", {}) is None


def test_map_events_list() -> None:
    reqs = map_kubectl_passthrough_request("events_list", {"namespace": "demo", "fieldSelector": "reason=Foo"})
    assert reqs is not None
    assert len(reqs) == 1
    assert reqs[0].attributes.verb == "list"
    assert reqs[0].attributes.resource == "events"
    assert reqs[0].attributes.namespace == "demo"


def test_map_events_list_without_namespace() -> None:
    assert map_kubectl_passthrough_request("events_list", {}) is None


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
    repository = _repository(ToolCallStatus.DENIED)
    service = _service(
        tmp_path, repository=repository, authorization=_authorization(allowed=True, reason="covered by SAR")
    )

    record = await service.submit_and_wait(req=_PODS_LIST, actor=_AGENT)

    assert record.status == ToolCallStatus.DENIED
    repository.submit.assert_awaited_once()
    kwargs = repository.submit.call_args.kwargs
    assert "covered by direct agent access" in kwargs["auto_approval_evaluation"]
    assert "Use your direct Haku Kubernetes proxy" in kwargs["auto_denial_reason"]


@pytest.mark.asyncio
async def test_kubectl_passthrough_falls_through_when_denied(tmp_path: Path) -> None:
    repository = _repository(ToolCallStatus.PENDING_APPROVAL)
    service = _service(
        tmp_path, repository=repository, authorization=_authorization(allowed=False, reason="not permitted")
    )

    record = await service.submit_and_wait(req=_PODS_LIST, actor=_AGENT)

    assert record.status == ToolCallStatus.PENDING_APPROVAL
    repository.submit.assert_awaited_once()
    kwargs = repository.submit.call_args.kwargs
    assert kwargs.get("auto_denial_reason") is None


if __name__ == "__main__":
    pytest_bazel.main()
