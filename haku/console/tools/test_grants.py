"""Integration tests for the unified ``grants`` MCP server over the real grant stores.

One server fronts both grant domains (#4918): every verb routes a ``domain``-tagged payload to the
per-domain PostgreSQL-backed grant service and tags the returned envelope. Assertions observe
durable state — created rows, source provenance, principal applicability — across both domains,
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
from fastmcp.exceptions import ToolError
from mcp.types import TextContent
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import haku.console.grants.http.models as http_models
import haku.console.grants.kubernetes.models as kubernetes_models
from haku.console.config import KubernetesAuthorizationConfig, KubernetesAuthorizationSubject
from haku.console.conftest import (
    # TestClient drives the app over httpx, imported inside starlette; gazelle cannot see it.
    # gazelle:include_dep @pypi//httpx
    DEFAULT_ACCESS_PROFILE_ID,
    default_agent_binding,
    insert_approved_tool_call,
    insert_live_session,
)
from haku.console.database_schema import Agent
from haku.console.grants.envelope import GrantStatus
from haku.console.grants.http.service import GrantService as HttpGrantService
from haku.console.grants.kubernetes.authorization import (
    KubernetesAuthorizationService,
    KubernetesAuthorizationSource,
    RequestAttributes,
    SubjectAccessReviewResult,
)
from haku.console.grants.kubernetes.service import GrantService as KubernetesGrantService
from haku.console.grants.principal import (
    AgentGrantPrincipal,
    GrantPrincipalKind,
    RequestPrincipal,
    SessionGrantPrincipal,
)
from haku.console.identity.agent_bearer_authority import AgentBearerAuthority
from haku.console.identity.enrollment import AgentEnrollmentService
from haku.console.mcp.execution import (
    AgentMcpExecutionCaller,
    McpExecutionContext,
    OperatorMcpExecutionCaller,
    mcp_execution_request_meta,
)
from haku.console.tools.grants import (
    GrantDomain,
    GrantsToolsService,
    HttpGrantRequest,
    HttpGrantView,
    KubernetesGrantRequest,
    KubernetesGrantView,
    build_mcp,
)
from haku.console.tools.kubernetes import KubernetesAccessCheck, KubernetesToolsService

_NOW = datetime(2026, 8, 27, tzinfo=UTC)
_K8S_SPEC = kubernetes_models.GrantSpec(
    scope=kubernetes_models.NamespacesGrantScope(namespaces=("demo",)),
    rules=(kubernetes_models.Rule(api_groups=("",), resources=("pods",), verbs=("get",)),),
)
_K8S_OTHER_SPEC = kubernetes_models.GrantSpec(
    scope=kubernetes_models.NamespacesGrantScope(namespaces=("other",)),
    rules=(kubernetes_models.Rule(api_groups=("apps",), resources=("deployments",), verbs=("patch",)),),
)
_HTTP_SPEC = http_models.GrantSpec(
    origin=http_models.HttpOrigin(scheme=http_models.HttpScheme.HTTPS, host="grocy.example", port=443),
    coverage=http_models.HttpRequestCoverage(methods=frozenset({http_models.HttpMethod.GET})),
)


def _kubernetes(spec: kubernetes_models.GrantSpec) -> KubernetesGrantRequest:
    return KubernetesGrantRequest(domain="kubernetes", spec=spec)


def _http(spec: http_models.GrantSpec) -> HttpGrantRequest:
    return HttpGrantRequest(domain="http", spec=spec)


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
    """One console app over a fresh migrated database, both grant stores wired into the server."""

    client: TestClient
    sessions: async_sessionmaker[AsyncSession]
    service: GrantsToolsService
    agent_id: UUID
    binding_id: UUID

    def call[T](self, func: Callable[..., Awaitable[T]], *args: Any) -> T:
        """Run one async step on the app's own event loop, where its engine lives."""
        assert self.client.portal is not None
        return self.client.portal.call(func, *args)

    def agent_context(self, *, session_id: UUID | None = None) -> McpExecutionContext:
        """A trusted Agent execution whose fresh ToolCall satisfies grant source provenance."""
        tool_call_id = self.call(
            partial(
                insert_approved_tool_call,
                self.sessions,
                binding_id=self.binding_id,
                now=_NOW,
                server_id="grants",
                session_id=session_id,
            )
        )
        return McpExecutionContext(
            caller=AgentMcpExecutionCaller(
                principal=RequestPrincipal(
                    agent_id=self.agent_id, session_id=session_id, access_profile_id=DEFAULT_ACCESS_PROFILE_ID
                )
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

    def live_session(self) -> UUID:
        return self.call(partial(insert_live_session, self.sessions, binding_id=self.binding_id, now=_NOW))


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
        kubernetes_grants = cast(KubernetesGrantService, app.state.kubernetes_grants)
        http_grants = cast(HttpGrantService, app.state.http_grants)
        agents = cast(AgentEnrollmentService, app.state.agent_enrollment_service)
        authorization = KubernetesAuthorizationService(
            config=KubernetesAuthorizationConfig(subjects_by_access_profile={DEFAULT_ACCESS_PROFILE_ID: _SUBJECT}),
            agent_bearer_authority=AgentBearerAuthority(()),
            grants=kubernetes_grants,
            sar_client=_FakeSubjectAccessReviews(),
        )
        assert client.portal is not None
        agent_id, binding_id = client.portal.call(default_agent_binding, sessions)
        yield _Console(
            client=client,
            sessions=sessions,
            service=GrantsToolsService(
                kubernetes=kubernetes_grants,
                http=http_grants,
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
    assert set(tools) == {"create_grant", "list_grants", "get_grant", "revoke_grants", "kubernetes_can_i"}
    for tool in tools.values():
        assert "context" not in tool.inputSchema.get("properties", {})
    assert set(tools["create_grant"].inputSchema["properties"]) == {"grants", "duration_seconds", "applies_to"}
    assert tools["create_grant"].inputSchema["properties"]["applies_to"]["default"] == "agent"
    # `list_grants` carries only the optional own-scope declaration; `self` is the sole value.
    assert set(tools["list_grants"].inputSchema["properties"]) == {"principal"}
    principal_schema = tools["list_grants"].inputSchema["properties"]["principal"]
    assert "self" in {branch.get("const") for branch in principal_schema["anyOf"]}
    assert set(tools["get_grant"].inputSchema["properties"]) == {"domain", "grant_id"}
    # One end-grants tool: an Agent omits owner_agent_id (relinquishes its own); an Operator names it.
    assert set(tools["revoke_grants"].inputSchema["properties"]) == {"domain", "grant_ids", "reason", "owner_agent_id"}
    assert set(tools["kubernetes_can_i"].inputSchema["properties"]) == {"requests"}
    # The create payload discriminates the two domains' capability specs by `domain`.
    branches = tools["create_grant"].inputSchema["properties"]["grants"]["items"]["oneOf"]
    assert {branch["properties"]["domain"]["const"] for branch in branches} == {"kubernetes", "http"}


def test_list_grants_is_actor_scoped_under_either_principal_declaration(console: _Console) -> None:
    context = console.agent_context()

    async def exercise() -> None:
        (view,) = await console.service.create_grants(
            context=context,
            requests=[_kubernetes(_K8S_SPEC)],
            duration_seconds=600,
            applies_to=GrantPrincipalKind.AGENT,
        )
        # `principal=self` and the omitted (reserved broader) read both return only the caller's own
        # grants today — the service is actor-scoped regardless; the scope arg only gates auto-approval.
        for principal in ("self", None):
            listed = await console.service.list_grants(context=context, principal=principal)
            assert [item.grant.grant_id for item in listed] == [view.grant.grant_id]

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
            assert allowed.data[0].source == KubernetesAuthorizationSource.SAR
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


def test_create_routes_each_domain_and_tags_the_returned_envelope(console: _Console) -> None:
    context = console.agent_context()

    async def exercise() -> None:
        (kubernetes_view,) = await console.service.create_grants(
            context=context,
            requests=[_kubernetes(_K8S_SPEC)],
            duration_seconds=600,
            applies_to=GrantPrincipalKind.AGENT,
        )
        (http_view,) = await console.service.create_grants(
            context=context, requests=[_http(_HTTP_SPEC)], duration_seconds=600, applies_to=GrantPrincipalKind.AGENT
        )
        assert isinstance(kubernetes_view, KubernetesGrantView)
        assert kubernetes_view.domain == "kubernetes"
        assert kubernetes_view.grant.scope == _K8S_SPEC.scope
        assert kubernetes_view.grant.rules == _K8S_SPEC.rules
        assert kubernetes_view.grant.owner_agent_id == console.agent_id
        assert kubernetes_view.grant.principal == AgentGrantPrincipal(agent_id=console.agent_id)
        assert kubernetes_view.grant.source_tool_call_id == context.tool_call_id
        assert isinstance(http_view, HttpGrantView)
        assert http_view.domain == "http"
        assert http_view.grant.spec == _HTTP_SPEC
        assert http_view.grant.status is GrantStatus.ACTIVE

        # list surfaces both domains, each tagged; get routes by the tag it returned.
        listed = await console.service.list_grants(context=context)
        assert {(view.domain, view.grant.grant_id) for view in listed} == {
            ("kubernetes", kubernetes_view.grant.grant_id),
            ("http", http_view.grant.grant_id),
        }
        assert (
            await console.service.get_grant(
                context=context, domain="kubernetes", grant_id=kubernetes_view.grant.grant_id
            )
        ) == kubernetes_view
        assert (
            await console.service.get_grant(context=context, domain="http", grant_id=http_view.grant.grant_id)
        ) == http_view

    console.call(exercise)


def test_create_rejects_a_call_that_straddles_domains(console: _Console) -> None:
    context = console.agent_context()

    async def exercise() -> None:
        with pytest.raises(ToolError, match="single domain"):
            await console.service.create_grants(
                context=context,
                requests=[_kubernetes(_K8S_SPEC), _http(_HTTP_SPEC)],
                duration_seconds=600,
                applies_to=GrantPrincipalKind.AGENT,
            )
        # Nothing was created in either domain.
        assert await console.service.list_grants(context=context) == []

    console.call(exercise)


def test_release_routes_by_domain_and_ends_in_the_supplied_order(console: _Console) -> None:
    context = console.agent_context()

    async def exercise() -> None:
        first, second = await console.service.create_grants(
            context=context,
            requests=[_kubernetes(_K8S_SPEC), _kubernetes(_K8S_OTHER_SPEC)],
            duration_seconds=600,
            applies_to=GrantPrincipalKind.AGENT,
        )
        # An Agent caller's revoke_grants relinquishes its own grants: the recorded fact is a release.
        released = await console.service.revoke_grants(
            context=context,
            domain="kubernetes",
            grant_ids=[second.grant.grant_id, first.grant.grant_id],
            reason="probe complete",
        )
        assert [view.grant.grant_id for view in released] == [second.grant.grant_id, first.grant.grant_id]
        assert all(view.grant.status is GrantStatus.RELEASED for view in released)
        refetched = await console.service.get_grant(context=context, domain="kubernetes", grant_id=first.grant.grant_id)
        assert refetched.grant.status is GrantStatus.RELEASED

    console.call(exercise)


@pytest.mark.parametrize(
    ("domain", "request_factory"),
    [
        pytest.param("kubernetes", lambda: _kubernetes(_K8S_SPEC), id="kubernetes"),
        pytest.param("http", lambda: _http(_HTTP_SPEC), id="http"),
    ],
)
def test_revoke_is_operator_direct_and_scoped_to_owned_agents(
    console: _Console, domain: GrantDomain, request_factory: Callable[[], KubernetesGrantRequest | HttpGrantRequest]
) -> None:
    agent_context = console.agent_context()
    # Resolved before entering the app's event loop: the portal cannot be re-entered from inside.
    operator_context = console.operator_context()

    async def exercise() -> None:
        (view,) = await console.service.create_grants(
            context=agent_context,
            requests=[request_factory()],
            duration_seconds=600,
            applies_to=GrantPrincipalKind.AGENT,
        )
        grant_id = view.grant.grant_id
        # An Agent caller never names an owner: naming owner_agent_id is rejected, and omitting it
        # relinquishes only its own grants (a release, covered above).
        with pytest.raises(PermissionError, match="may not name owner_agent_id"):
            await console.service.revoke_grants(
                context=agent_context, domain=domain, owner_agent_id=console.agent_id, grant_ids=[grant_id], reason="no"
            )
        # A foreign Operator does not see this Agent at all.
        with pytest.raises(LookupError):
            await console.service.revoke_grants(
                context=_foreign_operator_context(),
                domain=domain,
                owner_agent_id=console.agent_id,
                grant_ids=[grant_id],
                reason="not yours",
            )
        (revoked,) = await console.service.revoke_grants(
            context=operator_context,
            domain=domain,
            owner_agent_id=console.agent_id,
            grant_ids=[grant_id],
            reason="operator revoked",
        )
        assert revoked.grant.status is GrantStatus.REVOKED
        assert revoked.grant.end_reason == "operator revoked"

    console.call(exercise)


@pytest.mark.parametrize(
    "operation",
    [
        pytest.param(
            lambda service: service.create_grants(
                context=_foreign_operator_context(),
                requests=[_kubernetes(_K8S_SPEC)],
                duration_seconds=60,
                applies_to=GrantPrincipalKind.AGENT,
            ),
            id="create",
        ),
        pytest.param(lambda service: service.list_grants(context=_foreign_operator_context()), id="list"),
        pytest.param(
            lambda service: service.get_grant(context=_foreign_operator_context(), domain="http", grant_id=UUID(int=2)),
            id="get",
        ),
    ],
)
def test_operator_cannot_mint_or_inspect_agent_grants(
    console: _Console, operation: Callable[[GrantsToolsService], Awaitable[object]]
) -> None:
    with pytest.raises(PermissionError):
        console.call(partial(operation, console.service))


def test_session_scope_binds_the_grant_to_the_exact_live_session(console: _Console) -> None:
    session_id = console.live_session()
    session_context = console.agent_context(session_id=session_id)
    static_context = console.agent_context()

    async def exercise() -> None:
        (session_view,) = await console.service.create_grants(
            context=session_context,
            requests=[_http(_HTTP_SPEC)],
            duration_seconds=600,
            applies_to=GrantPrincipalKind.SESSION,
        )
        assert session_view.grant.principal == SessionGrantPrincipal(session_id=session_id)
        (agent_view,) = await console.service.create_grants(
            context=static_context,
            requests=[_http(_HTTP_SPEC)],
            duration_seconds=600,
            applies_to=GrantPrincipalKind.AGENT,
        )
        # The exact session may exercise both; a static-credential execution never sees the session grant.
        assert {view.grant.grant_id for view in await console.service.list_grants(context=session_context)} == {
            session_view.grant.grant_id,
            agent_view.grant.grant_id,
        }
        assert [view.grant.grant_id for view in await console.service.list_grants(context=static_context)] == [
            agent_view.grant.grant_id
        ]

    console.call(exercise)


if __name__ == "__main__":
    pytest_bazel.main()
