"""Integration tests for the ``grants`` MCP server over the real Kubernetes grant store.

Assertions observe durable state — created rows, source provenance, principal applicability —
rather than call forwarding.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Any, cast
from uuid import UUID

import pytest
import pytest_bazel
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastmcp import Client
from mcp.types import TextContent
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import haku.console.grants.kubernetes.models as kubernetes_models
from haku.console.config import KubernetesAuthorizationConfig, KubernetesAuthorizationSubject
from haku.console.conftest import (
    # TestClient drives the app over httpx, imported inside starlette; gazelle cannot see it.
    # gazelle:include_dep @pypi//httpx
    DEFAULT_ACCESS_PROFILE_ID,
    default_agent_binding,
    insert_approved_tool_call,
)
from haku.console.database_schema import Agent
from haku.console.grants.catalog import ConfigFileGrantSource, DatabaseGrantSource, GrantCatalog
from haku.console.grants.envelope import GrantStatus
from haku.console.grants.kubernetes.authorization import RequestAttributes, SubjectAccessReviewResult
from haku.console.grants.kubernetes.authorization_service import KubernetesAuthorizationService
from haku.console.grants.kubernetes.service import GrantService
from haku.console.grants.principal import (
    AccessProfileGrantPrincipal,
    AgentGrantPrincipal,
    GrantPrincipalInput,
    RequestPrincipal,
)
from haku.console.identity.agent_bearer_authority import AgentBearerAuthority
from haku.console.identity.enrollment import AgentEnrollmentService
from haku.console.mcp.execution import (
    AgentMcpExecutionCaller,
    McpExecutionContext,
    OperatorMcpExecutionCaller,
    mcp_execution_request_meta,
)
from haku.console.tools.grants import GrantsToolsService, build_mcp
from haku.console.tools.kubernetes import KubernetesAccessCheck, KubernetesToolsService
from haku.grants.authorization import GrantSourceKind

_NOW = datetime(2026, 8, 27, tzinfo=UTC)
_K8S_SPEC = kubernetes_models.GrantSpec(
    scope=kubernetes_models.NamespacesGrantScope(namespaces={"demo"}),
    rules=(kubernetes_models.Rule(api_groups={""}, resources={"pods"}, verbs={"get"}),),
)
_K8S_OTHER_SPEC = kubernetes_models.GrantSpec(
    scope=kubernetes_models.NamespacesGrantScope(namespaces={"other"}),
    rules=(kubernetes_models.Rule(api_groups={"apps"}, resources={"deployments"}, verbs={"patch"}),),
)

_CAN_I_REQUEST = RequestAttributes(
    resource_request=True,
    verb="get",
    api_version="v1",
    namespace="demo",
    resource="pods",
    path="/api/v1/namespaces/demo/pods",
)
_SUBJECT = KubernetesAuthorizationSubject(username="haku-agent-subject", groups=("haku-agents",))


class _FakeSubjectAccessReviews:
    """Stands in for the in-cluster Kubernetes SAR API — the one external boundary for can_i."""

    def __init__(self) -> None:
        self.allowed = True
        self.reason: str | None = "RBAC: allowed"

    async def review(
        self, *, subject: KubernetesAuthorizationSubject, attributes: RequestAttributes
    ) -> SubjectAccessReviewResult:
        return SubjectAccessReviewResult(allowed=self.allowed, reason=self.reason)

    async def aclose(self) -> None:
        pass


@dataclass(frozen=True, slots=True)
class _Console:
    """One console app over a fresh migrated database, its Kubernetes grant store wired into the server."""

    client: TestClient
    sessions: async_sessionmaker[AsyncSession]
    service: GrantsToolsService
    agent_id: UUID
    binding_id: UUID

    def call[T](self, func: Callable[..., Awaitable[T]], *args: Any) -> T:
        """Run one async step on the app's own event loop, where its engine lives."""
        assert self.client.portal is not None
        return cast(T, self.client.portal.call(func, *args))

    def agent_context(self) -> McpExecutionContext:
        """A trusted Agent execution whose fresh ToolCall satisfies grant source provenance."""
        tool_call_id = self.call(
            partial(insert_approved_tool_call, self.sessions, binding_id=self.binding_id, now=_NOW, server_id="grants")
        )
        return McpExecutionContext(
            caller=AgentMcpExecutionCaller(
                principal=RequestPrincipal(agent_id=self.agent_id, access_profile_id=DEFAULT_ACCESS_PROFILE_ID)
            ),
            tool_call_id=tool_call_id,
            approving_operator_id=None,
            approval_policy_id=None,
        )

    def operator_context(self) -> McpExecutionContext:
        """The Operator owning the seeded default Agent, as a direct MCP execution."""

        async def owner_operator_id() -> UUID:
            async with self.sessions() as session:
                operator_id = await session.scalar(
                    select(Agent.owner_operator_id).where(Agent.agent_id == self.agent_id)
                )
                assert operator_id is not None
                return operator_id

        return McpExecutionContext(
            caller=OperatorMcpExecutionCaller(operator_id=self.call(owner_operator_id)),
            tool_call_id=None,
            approving_operator_id=None,
            approval_policy_id=None,
        )


def _foreign_operator_context() -> McpExecutionContext:
    return McpExecutionContext(
        caller=OperatorMcpExecutionCaller(operator_id=UUID(int=9)),
        tool_call_id="tc_operator",
        approving_operator_id=None,
        approval_policy_id=None,
    )


@pytest.fixture
def console(make_client: Callable[..., Any]) -> Iterator[_Console]:
    with make_client() as client:
        app = cast(FastAPI, client.app)
        sessions = cast(async_sessionmaker[AsyncSession], app.state.db_sessions)
        kubernetes_grants = cast(GrantService, app.state.kubernetes_grants)
        agents = cast(AgentEnrollmentService, app.state.agent_enrollment_service)
        catalog = GrantCatalog(
            kubernetes_grants=kubernetes_grants,
            kubernetes_config=KubernetesAuthorizationConfig(
                subjects_by_access_profile={DEFAULT_ACCESS_PROFILE_ID: _SUBJECT}
            ),
            sar_client=_FakeSubjectAccessReviews(),
        )
        authorization = KubernetesAuthorizationService(agent_bearer_authority=AgentBearerAuthority(()), catalog=catalog)
        assert client.portal is not None
        agent_id, binding_id = client.portal.call(default_agent_binding, sessions)
        yield _Console(
            client=client,
            sessions=sessions,
            service=GrantsToolsService(
                kubernetes=kubernetes_grants,
                catalog=catalog,
                agents=agents,
                can_i=KubernetesToolsService(authorization=authorization),
            ),
            agent_id=agent_id,
            binding_id=binding_id,
        )


def test_server_exposes_exact_stable_tool_set_without_context_argument(console: _Console) -> None:
    async def list_tools() -> list[Any]:
        async with Client(build_mcp(console.service)) as client:
            return list(await client.list_tools())

    tools = {tool.name: tool for tool in console.call(list_tools)}
    assert set(tools) == {"create_grant", "list_grants", "get_grant", "revoke_grants", "kubernetes_can_i", "whoami"}
    for tool in tools.values():
        assert "context" not in tool.input_schema.get("properties", {})
    # whoami is a pure identity read: no arguments beyond the hidden execution context.
    assert tools["whoami"].input_schema.get("properties", {}) == {}
    assert set(tools["create_grant"].input_schema["properties"]) == {"grants", "duration_seconds", "principal"}
    assert set(tools["list_grants"].input_schema["properties"]) == {"principal", "include_inactive"}
    assert set(tools["get_grant"].input_schema["properties"]) == {"grant_id"}
    # One end-grants tool: an Agent omits owner_agent_id (relinquishes its own); an Operator names it.
    assert set(tools["revoke_grants"].input_schema["properties"]) == {"grant_ids", "reason", "owner_agent_id"}
    assert set(tools["kubernetes_can_i"].input_schema["properties"]) == {"requests"}


def test_list_grants_resolves_self_and_named_subjects(console: _Console) -> None:
    context = console.agent_context()

    async def exercise() -> None:
        (view,) = await console.service.create_grants(
            context=context,
            requests=[_K8S_SPEC],
            duration_seconds=600,
            principal=AgentGrantPrincipal(agent_id=console.agent_id),
        )
        self_grants = await console.service.list_grants(context=context, principal="self")
        assert [item.source.id for item in self_grants if isinstance(item.source, DatabaseGrantSource)] == [
            view.grant_id
        ]
        all_grants = await console.service.list_grants(context=context)
        assert [item.source.id for item in all_grants if isinstance(item.source, DatabaseGrantSource)] == [
            view.grant_id
        ]
        assert [grant.source.entry_id for grant in all_grants if isinstance(grant.source, ConfigFileGrantSource)] == [
            f"kubernetes-profile:{DEFAULT_ACCESS_PROFILE_ID}"
        ]
        named = await console.service.list_grants(
            context=context, principal=AgentGrantPrincipal(agent_id=console.agent_id)
        )
        assert [item.source.id for item in named if isinstance(item.source, DatabaseGrantSource)] == [view.grant_id]
        profile_grants = await console.service.list_grants(
            context=context, principal=AccessProfileGrantPrincipal(access_profile_id=DEFAULT_ACCESS_PROFILE_ID)
        )
        assert [
            grant.source.entry_id for grant in profile_grants if isinstance(grant.source, ConfigFileGrantSource)
        ] == [f"kubernetes-profile:{DEFAULT_ACCESS_PROFILE_ID}"]

    console.call(exercise)


def test_agent_can_request_a_grant_for_any_access_profile(console: _Console) -> None:
    context = console.agent_context()
    principal = AccessProfileGrantPrincipal(access_profile_id="other-profile")

    async def exercise() -> None:
        (view,) = await console.service.create_grants(
            context=context, requests=[_K8S_SPEC], duration_seconds=600, principal=principal
        )
        assert view.principal == principal

    console.call(exercise)


def test_list_grants_includes_database_history_only_when_requested(console: _Console) -> None:
    context = console.agent_context()

    async def exercise() -> None:
        (view,) = await console.service.create_grants(
            context=context,
            requests=[_K8S_SPEC],
            duration_seconds=600,
            principal=AgentGrantPrincipal(agent_id=console.agent_id),
        )
        await console.service.revoke_grants(context=context, grant_ids=[view.grant_id], reason=None)
        scopes: tuple[GrantPrincipalInput | None, ...] = ("self", None, AgentGrantPrincipal(agent_id=console.agent_id))
        for principal in scopes:
            current = await console.service.list_grants(context=context, principal=principal)
            history = await console.service.list_grants(context=context, principal=principal, include_inactive=True)
            assert not [grant for grant in current if isinstance(grant.source, DatabaseGrantSource)]
            assert [grant.source.id for grant in history if isinstance(grant.source, DatabaseGrantSource)] == [
                view.grant_id
            ]
        assert [
            grant.source.entry_id
            for grant in await console.service.list_grants(context=context)
            if isinstance(grant.source, ConfigFileGrantSource)
        ] == [f"kubernetes-profile:{DEFAULT_ACCESS_PROFILE_ID}"]
        assert [
            grant.source.entry_id
            for grant in await console.service.list_grants(context=context, include_inactive=True)
            if isinstance(grant.source, ConfigFileGrantSource)
        ] == [f"kubernetes-profile:{DEFAULT_ACCESS_PROFILE_ID}"]

    console.call(exercise)


def test_kubernetes_can_i_rides_the_grants_server(console: _Console) -> None:
    context = console.agent_context()

    async def exercise() -> None:
        async with Client(build_mcp(console.service)) as client:
            meta = mcp_execution_request_meta(context)
            allowed = await client.call_tool(
                "kubernetes_can_i",
                {"requests": [KubernetesAccessCheck(attributes=_CAN_I_REQUEST).model_dump()]},
                meta=meta,
            )
            assert allowed.data[0].allowed is True
            # The MCP client deserializes the StrEnum as its wire string, so compare by value.
            assert allowed.data[0].source == GrantSourceKind.CONFIG_FILE
            # An ambiguous unnamespaced request surfaces as one clean ToolError line, not a trace.
            ambiguous = await client.call_tool(
                "kubernetes_can_i",
                {"requests": [{"attributes": {"resource_request": True, "verb": "list", "resource": "pods"}}]},
                meta=meta,
                raise_on_error=False,
            )
            assert ambiguous.is_error
            block = ambiguous.content[0]
            assert isinstance(block, TextContent)
            assert "\n" not in block.text
            assert "requests[0]" in block.text

    console.call(exercise)


def test_whoami_returns_the_callers_resolved_console_identity(console: _Console) -> None:
    """whoami echoes the trusted execution caller Console resolved: an Agent's request principal
    (agent_id + access profile) or a direct Operator's operator_id. It takes no arguments and reads
    identity only from trusted request metadata, so the value round-trips through the MCP wire back
    to exactly the caller the execution context carried."""
    agent_context = console.agent_context()
    operator_context = console.operator_context()

    async def exercise() -> None:
        async with Client(build_mcp(console.service)) as client:
            for context, variant in (
                (agent_context, AgentMcpExecutionCaller),
                (operator_context, OperatorMcpExecutionCaller),
            ):
                result = await client.call_tool("whoami", {}, meta=mcp_execution_request_meta(context))
                payload = result.structured_content
                assert isinstance(payload, dict)
                # FastMCP wraps a non-object (here discriminated-union) return in {"result": …};
                # unwrap that envelope when present, then reparse into the concrete caller variant.
                inner = payload["result"] if set(payload) == {"result"} else payload
                assert variant.model_validate(inner) == context.caller

    console.call(exercise)


def test_create_grant_tags_the_returned_envelope(console: _Console) -> None:
    context = console.agent_context()

    async def exercise() -> None:
        (kubernetes_view,) = await console.service.create_grants(
            context=context,
            requests=[_K8S_SPEC],
            duration_seconds=600,
            principal=AgentGrantPrincipal(agent_id=console.agent_id),
        )
        assert kubernetes_view.scope == _K8S_SPEC.scope
        assert kubernetes_view.rules == _K8S_SPEC.rules
        assert kubernetes_view.owner_agent_id == console.agent_id
        assert kubernetes_view.principal == AgentGrantPrincipal(agent_id=console.agent_id)
        assert kubernetes_view.source_tool_call_id == context.tool_call_id

        listed = await console.service.list_grants(context=context)
        assert {
            (view.coverage.kind, view.source.id) for view in listed if isinstance(view.source, DatabaseGrantSource)
        } == {("kubernetes_rules", kubernetes_view.grant_id)}
        kubernetes_grant = await console.service.get_grant(context=context, grant_id=kubernetes_view.grant_id)
        assert kubernetes_grant in listed

    console.call(exercise)


def test_release_ends_grants_in_the_supplied_order(console: _Console) -> None:
    context = console.agent_context()

    async def exercise() -> None:
        first, second = await console.service.create_grants(
            context=context,
            requests=[_K8S_SPEC, _K8S_OTHER_SPEC],
            duration_seconds=600,
            principal=AgentGrantPrincipal(agent_id=console.agent_id),
        )
        # An Agent caller's revoke_grants ends its own grants.
        released = await console.service.revoke_grants(
            context=context, grant_ids=[second.grant_id, first.grant_id], reason="probe complete"
        )
        assert [view.grant_id for view in released] == [second.grant_id, first.grant_id]
        assert all(view.status is GrantStatus.ENDED for view in released)
        refetched = await console.service.get_grant(context=context, grant_id=first.grant_id)
        assert refetched.validity.status is GrantStatus.ENDED

    console.call(exercise)


def test_revoke_is_operator_direct_and_scoped_to_owned_agents(console: _Console) -> None:
    agent_context = console.agent_context()
    # Resolved before entering the app's event loop: the portal cannot be re-entered from inside.
    operator_context = console.operator_context()

    async def exercise() -> None:
        (view,) = await console.service.create_grants(
            context=agent_context,
            requests=[_K8S_SPEC],
            duration_seconds=600,
            principal=AgentGrantPrincipal(agent_id=console.agent_id),
        )
        grant_id = view.grant_id
        # An Agent caller never names an owner: naming owner_agent_id is rejected, and omitting it
        # ends only its own grants.
        with pytest.raises(PermissionError, match="may not name owner_agent_id"):
            await console.service.revoke_grants(
                context=agent_context, owner_agent_id=console.agent_id, grant_ids=[grant_id], reason="no"
            )
        # A foreign Operator does not see this Agent at all.
        with pytest.raises(LookupError):
            await console.service.revoke_grants(
                context=_foreign_operator_context(),
                owner_agent_id=console.agent_id,
                grant_ids=[grant_id],
                reason="not yours",
            )
        (revoked,) = await console.service.revoke_grants(
            context=operator_context, owner_agent_id=console.agent_id, grant_ids=[grant_id], reason="operator revoked"
        )
        assert revoked.status is GrantStatus.ENDED
        assert revoked.end_reason == "operator revoked"

    console.call(exercise)


@pytest.mark.parametrize(
    "operation",
    [
        pytest.param(
            lambda service: service.create_grants(
                context=_foreign_operator_context(),
                requests=[_K8S_SPEC],
                duration_seconds=60,
                principal=AgentGrantPrincipal(agent_id=UUID(int=1)),
            ),
            id="create",
        ),
        pytest.param(lambda service: service.list_grants(context=_foreign_operator_context()), id="list"),
        pytest.param(
            lambda service: service.get_grant(context=_foreign_operator_context(), grant_id=UUID(int=2)), id="get"
        ),
    ],
)
def test_operator_cannot_mint_or_inspect_agent_grants(
    console: _Console, operation: Callable[[GrantsToolsService], Awaitable[object]]
) -> None:
    with pytest.raises(PermissionError):
        console.call(partial(operation, console.service))


if __name__ == "__main__":
    pytest_bazel.main()
