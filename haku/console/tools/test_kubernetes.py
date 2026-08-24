from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import pytest_bazel
from fastmcp import Client
from pydantic import ValidationError

from haku.console.kubernetes_authorization import (
    AuthorizationResponse,
    KubernetesAuthorizationSource,
    RequestAttributes,
)
from haku.console.kubernetes_grant_models import (
    KubernetesGrant,
    KubernetesGrantScopeKind,
    KubernetesGrantSpec,
    KubernetesGrantStatus,
    KubernetesNamespacesGrantScope,
    KubernetesRule,
)
from haku.console.mcp_execution import AgentMcpExecutionCaller, McpExecutionContext, OperatorMcpExecutionCaller
from haku.console.tools.kubernetes import KubernetesAccessCheck, KubernetesToolsService, build_mcp

_AGENT = UUID("10000000-0000-4000-8000-000000000001")
_GRANT = UUID("20000000-0000-4000-8000-000000000002")
_NOW = datetime(2026, 8, 20, tzinfo=UTC)
_SCOPE = KubernetesNamespacesGrantScope(namespaces=("demo",))
_RULE = KubernetesRule(api_groups=("",), resources=("pods",), verbs=("get",))
_REQUEST = RequestAttributes(
    resource_request=True,
    verb="get",
    api_version="v1",
    namespace="demo",
    resource="pods",
    path="/api/v1/namespaces/demo/pods",
)


def _agent_context() -> McpExecutionContext:
    return McpExecutionContext(
        caller=AgentMcpExecutionCaller(agent_id=_AGENT, access_profile_id="public-coder"),
        tool_call_id="tc_create_grant",
    )


def _operator_context() -> McpExecutionContext:
    return McpExecutionContext(caller=OperatorMcpExecutionCaller(operator_id=UUID(int=9)), tool_call_id="tc_operator")


def _grant() -> KubernetesGrant:
    return KubernetesGrant(
        grant_id=_GRANT,
        agent_id=_AGENT,
        source_tool_call_id="tc_create_grant",
        scope=_SCOPE,
        rules=(_RULE,),
        status=KubernetesGrantStatus.ACTIVE,
        created_at=_NOW,
        expires_at=datetime(2026, 8, 20, 1, tzinfo=UTC),
    )


def _service() -> tuple[KubernetesToolsService, AsyncMock, AsyncMock]:
    grants = AsyncMock()
    authorization = AsyncMock()
    grants.create_grants.return_value = (_grant(),)
    grants.list_grants.return_value = (_grant(),)
    grants.get_grant.return_value = _grant()
    grants.release_grants.return_value = (_grant(),)
    authorization.authorize_agent.return_value = AuthorizationResponse(
        allowed=True, reason="standing", source=KubernetesAuthorizationSource.SAR, decision_id="sar:decision"
    )
    return KubernetesToolsService(grants=grants, authorization=authorization), grants, authorization


@pytest.mark.asyncio
async def test_server_exposes_exact_stable_tool_set_without_context_argument() -> None:
    service, _, _ = _service()
    async with Client(build_mcp(service)) as client:
        tools = await client.list_tools()
    assert {tool.name for tool in tools} == {"can_i", "create_grant", "list_grants", "get_grant", "release_grants"}
    for tool in tools:
        assert "context" not in tool.inputSchema.get("properties", {})
    create_grant = next(tool for tool in tools if tool.name == "create_grant")
    assert set(create_grant.inputSchema["properties"]) == {"grants", "duration_seconds"}
    assert create_grant.inputSchema["properties"]["grants"]["minItems"] == 1
    assert create_grant.inputSchema["properties"]["grants"]["maxItems"] == 32
    release_grants = next(tool for tool in tools if tool.name == "release_grants")
    assert set(release_grants.inputSchema["properties"]) == {"grant_ids", "reason"}
    assert release_grants.inputSchema["properties"]["grant_ids"]["minItems"] == 1
    assert release_grants.inputSchema["properties"]["grant_ids"]["maxItems"] == 32


@pytest.mark.asyncio
async def test_create_uses_trusted_agent_current_tool_call_and_exact_grants() -> None:
    service, grants, _ = _service()
    requested = [
        KubernetesGrantSpec(scope=_SCOPE, rules=(_RULE,)),
        KubernetesGrantSpec(
            scope=KubernetesNamespacesGrantScope(namespaces=("other",)),
            rules=(KubernetesRule(api_groups=("apps",), resources=("deployments",), verbs=("patch",)),),
        ),
    ]
    await service.create_grants(context=_agent_context(), grants=requested, duration_seconds=60)
    kwargs = grants.create_grants.await_args.kwargs
    assert kwargs["agent_id"] == _AGENT
    assert kwargs["source_tool_call_id"] == "tc_create_grant"
    assert kwargs["grants"] == requested


@pytest.mark.asyncio
async def test_operator_cannot_mint_or_inspect_agent_grants() -> None:
    service, _, _ = _service()
    with pytest.raises(PermissionError):
        await service.create_grants(
            context=_operator_context(), grants=[KubernetesGrantSpec(scope=_SCOPE, rules=(_RULE,))], duration_seconds=60
        )
    with pytest.raises(PermissionError):
        await service.list_grants(context=_operator_context())
    with pytest.raises(PermissionError):
        await service.get_grant(context=_operator_context(), grant_id=_GRANT)
    with pytest.raises(PermissionError):
        await service.release_grants(context=_operator_context(), grant_ids=[_GRANT])


@pytest.mark.asyncio
async def test_release_uses_trusted_agent_and_supplied_grant_order() -> None:
    service, grants, _ = _service()
    other = UUID("20000000-0000-4000-8000-000000000003")
    grants.release_grants.return_value = (_grant(), _grant().model_copy(update={"grant_id": other}))

    result = await service.release_grants(context=_agent_context(), grant_ids=[_GRANT, other], reason="probe complete")

    assert [grant.grant_id for grant in result] == [_GRANT, other]
    grants.release_grants.assert_awaited_once_with(agent_id=_AGENT, grant_ids=[_GRANT, other], reason="probe complete")


@pytest.mark.asyncio
async def test_can_i_uses_shared_agent_evaluator_and_returns_source() -> None:
    service, _, authorization = _service()
    result = await service.can_i(context=_agent_context(), requests=[KubernetesAccessCheck(attributes=_REQUEST)])
    assert result[0].allowed is True
    assert result[0].source is KubernetesAuthorizationSource.SAR
    kwargs = authorization.authorize_agent.await_args.kwargs
    assert kwargs["agent_id"] == _AGENT
    assert kwargs["access_profile_id"] == "public-coder"
    request = kwargs["request"]
    assert request.attributes == _REQUEST
    assert request.required_scope == _SCOPE
    assert request.required_rules == [_RULE]


def test_can_i_requires_explicit_scope_for_unnamespaced_resource_request() -> None:
    attributes = RequestAttributes(
        resource_request=True, verb="list", api_version="v1", resource="pods", path="/api/v1/pods"
    )
    with pytest.raises(ValidationError, match="all_namespaces or cluster"):
        KubernetesAccessCheck(attributes=attributes)
    check = KubernetesAccessCheck(
        attributes=attributes, unnamespaced_resource_kind=KubernetesGrantScopeKind.ALL_NAMESPACES
    )
    assert check.unnamespaced_resource_kind is KubernetesGrantScopeKind.ALL_NAMESPACES


if __name__ == "__main__":
    pytest_bazel.main()
