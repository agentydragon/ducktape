"""Integration tests for the Kubernetes MCP tools over the real grant store.

The tools service runs against the app's PostgreSQL-backed grant service, so every assertion
observes durable state — created rows, source provenance, principal applicability — rather than
call forwarding. The only stand-in is the SubjectAccessReview client: the in-cluster Kubernetes
API is a genuine external boundary.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.agent_bearer_authority import AgentBearerAuthority
from haku.console.config import KubernetesAuthorizationConfig, KubernetesAuthorizationSubject
from haku.console.conftest import default_agent_binding, insert_approved_tool_call, insert_live_session
from haku.console.grant_principal import (
    AgentGrantPrincipal,
    GrantPrincipalKind,
    RequestPrincipal,
    SessionGrantPrincipal,
)
from haku.console.kubernetes_authorization import (
    KubernetesAuthorizationService,
    KubernetesAuthorizationSource,
    RequestAttributes,
    SubjectAccessReviewResult,
)
from haku.console.kubernetes_grant_models import (
    KubernetesClusterGrantScope,
    KubernetesGrantScopeKind,
    KubernetesGrantSpec,
    KubernetesGrantStatus,
    KubernetesNamespacesGrantScope,
    KubernetesRule,
)
from haku.console.kubernetes_grant_service import KubernetesGrantService
from haku.console.mcp_execution import (
    AgentMcpExecutionCaller,
    McpExecutionContext,
    OperatorMcpExecutionCaller,
    mcp_execution_request_meta,
)
from haku.console.tools.kubernetes import KubernetesAccessCheck, KubernetesToolsService, build_mcp

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
_SUBJECT = KubernetesAuthorizationSubject(username="haku-agent-subject", groups=("haku-agents",))
# The access profile `make_client` assigns its seeded default static agent.
_PROFILE = "no_auto_approval"


class _FakeSubjectAccessReviews:
    """Stands in for the in-cluster Kubernetes SAR API — the one external boundary here."""

    def __init__(self) -> None:
        self.allowed = False
        self.reason: str | None = "RBAC: access denied"
        self.reviews: list[tuple[KubernetesAuthorizationSubject, RequestAttributes]] = []

    async def review(
        self, *, subject: KubernetesAuthorizationSubject, attributes: RequestAttributes
    ) -> SubjectAccessReviewResult:
        self.reviews.append((subject, attributes))
        return SubjectAccessReviewResult(allowed=self.allowed, reason=self.reason)

    async def aclose(self) -> None:
        pass


@dataclass(frozen=True, slots=True)
class _Console:
    """One console app over a fresh migrated database, its grant store wired into the tools."""

    client: TestClient
    sessions: async_sessionmaker[AsyncSession]
    service: KubernetesToolsService
    sar: _FakeSubjectAccessReviews
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
                insert_approved_tool_call, self.sessions, binding_id=self.binding_id, now=_NOW, session_id=session_id
            )
        )
        return McpExecutionContext(
            caller=AgentMcpExecutionCaller(
                principal=RequestPrincipal(agent_id=self.agent_id, session_id=session_id, access_profile_id=_PROFILE)
            ),
            tool_call_id=tool_call_id,
            approving_operator_id=None,
            approval_policy_id=None,
        )

    def live_session(self) -> UUID:
        return self.call(partial(insert_live_session, self.sessions, binding_id=self.binding_id, now=_NOW))


def _operator_context() -> McpExecutionContext:
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
        grants = cast(KubernetesGrantService, app.state.kubernetes_grants)
        sar = _FakeSubjectAccessReviews()
        authorization = KubernetesAuthorizationService(
            config=KubernetesAuthorizationConfig(subjects_by_access_profile={_PROFILE: _SUBJECT}),
            # The trusted in-process path never resolves a bearer; no sources states that.
            agent_bearer_authority=AgentBearerAuthority(()),
            grants=grants,
            sar_client=sar,
        )
        assert client.portal is not None
        agent_id, binding_id = client.portal.call(default_agent_binding, sessions)
        yield _Console(
            client=client,
            sessions=sessions,
            service=KubernetesToolsService(grants=grants, authorization=authorization),
            sar=sar,
            agent_id=agent_id,
            binding_id=binding_id,
        )


def test_server_exposes_exact_stable_tool_set_without_context_argument(console: _Console) -> None:
    async def list_tools() -> list[Any]:
        async with Client(build_mcp(console.service)) as client:
            return list(await client.list_tools())

    tools = console.call(list_tools)
    assert {tool.name for tool in tools} == {"can_i", "create_grant", "list_grants", "get_grant", "release_grants"}
    for tool in tools:
        assert "context" not in tool.inputSchema.get("properties", {})
    create_grant = next(tool for tool in tools if tool.name == "create_grant")
    assert set(create_grant.inputSchema["properties"]) == {"grants", "duration_seconds", "applies_to"}
    assert create_grant.inputSchema["properties"]["applies_to"]["default"] == "agent"
    assert create_grant.inputSchema["properties"]["grants"]["minItems"] == 1
    assert create_grant.inputSchema["properties"]["grants"]["maxItems"] == 32
    release_grants = next(tool for tool in tools if tool.name == "release_grants")
    assert set(release_grants.inputSchema["properties"]) == {"grant_ids", "reason"}
    assert release_grants.inputSchema["properties"]["grant_ids"]["minItems"] == 1
    assert release_grants.inputSchema["properties"]["grant_ids"]["maxItems"] == 32


def test_create_persists_trusted_identity_provenance_and_exact_grants(console: _Console) -> None:
    context = console.agent_context()
    requested = [
        KubernetesGrantSpec(scope=_SCOPE, rules=(_RULE,)),
        KubernetesGrantSpec(
            scope=KubernetesNamespacesGrantScope(namespaces=("other",)),
            rules=(KubernetesRule(api_groups=("apps",), resources=("deployments",), verbs=("patch",)),),
        ),
    ]

    async def exercise() -> None:
        created = await console.service.create_grants(context=context, grants=requested, duration_seconds=600)
        assert [grant.scope for grant in created] == [spec.scope for spec in requested]
        assert [grant.rules for grant in created] == [spec.rules for spec in requested]
        for grant in created:
            assert grant.owner_agent_id == console.agent_id
            assert grant.principal == AgentGrantPrincipal(agent_id=console.agent_id)
            assert grant.source_tool_call_id == context.tool_call_id
            assert grant.status is KubernetesGrantStatus.ACTIVE
        # One atomic set: shared timestamps, expiry bounded by the requested duration.
        assert len({(grant.created_at, grant.expires_at) for grant in created}) == 1
        assert timedelta() < created[0].expires_at - created[0].created_at <= timedelta(seconds=600)
        assert set(await console.service.list_grants(context=context)) == set(created)
        assert await console.service.get_grant(context=context, grant_id=created[0].grant_id) == created[0]

    console.call(exercise)


@pytest.mark.parametrize(
    "operation",
    [
        pytest.param(
            lambda service: service.create_grants(
                context=_operator_context(),
                grants=[KubernetesGrantSpec(scope=_SCOPE, rules=(_RULE,))],
                duration_seconds=60,
            ),
            id="create",
        ),
        pytest.param(lambda service: service.list_grants(context=_operator_context()), id="list"),
        pytest.param(lambda service: service.get_grant(context=_operator_context(), grant_id=UUID(int=2)), id="get"),
        pytest.param(
            lambda service: service.release_grants(context=_operator_context(), grant_ids=[UUID(int=2)]), id="release"
        ),
    ],
)
def test_operator_cannot_mint_or_inspect_agent_grants(
    console: _Console, operation: Callable[[KubernetesToolsService], Awaitable[object]]
) -> None:
    with pytest.raises(PermissionError):
        console.call(partial(operation, console.service))


def test_session_scope_binds_the_grant_to_the_exact_live_session(console: _Console) -> None:
    session_id = console.live_session()
    session_context = console.agent_context(session_id=session_id)
    static_context = console.agent_context()

    async def exercise() -> None:
        (session_grant,) = await console.service.create_grants(
            context=session_context,
            grants=[KubernetesGrantSpec(scope=_SCOPE, rules=(_RULE,))],
            duration_seconds=600,
            applies_to=GrantPrincipalKind.SESSION,
        )
        assert session_grant.principal == SessionGrantPrincipal(session_id=session_id)
        (agent_grant,) = await console.service.create_grants(
            context=static_context, grants=[KubernetesGrantSpec(scope=_SCOPE, rules=(_RULE,))], duration_seconds=600
        )
        # The exact session may exercise both; a static-credential execution of the same Agent
        # never sees the session-scoped grant.
        assert set(await console.service.list_grants(context=session_context)) == {session_grant, agent_grant}
        assert await console.service.list_grants(context=static_context) == (agent_grant,)

    console.call(exercise)


def test_create_session_scope_rejects_static_agent_context(console: _Console) -> None:
    context = console.agent_context()

    async def exercise() -> None:
        with pytest.raises(PermissionError, match="live session-authenticated"):
            await console.service.create_grants(
                context=context,
                grants=[KubernetesGrantSpec(scope=_SCOPE, rules=(_RULE,))],
                duration_seconds=600,
                applies_to=GrantPrincipalKind.SESSION,
            )
        assert await console.service.list_grants(context=context) == ()

    console.call(exercise)


def test_release_ends_grants_in_the_supplied_order(console: _Console) -> None:
    context = console.agent_context()

    async def exercise() -> None:
        first, second = await console.service.create_grants(
            context=context,
            grants=[
                KubernetesGrantSpec(scope=_SCOPE, rules=(_RULE,)),
                KubernetesGrantSpec(scope=KubernetesNamespacesGrantScope(namespaces=("other",)), rules=(_RULE,)),
            ],
            duration_seconds=600,
        )
        released = await console.service.release_grants(
            context=context, grant_ids=[second.grant_id, first.grant_id], reason="probe complete"
        )
        assert [grant.grant_id for grant in released] == [second.grant_id, first.grant_id]
        assert all(grant.status is KubernetesGrantStatus.RELEASED for grant in released)
        assert {grant.end_reason for grant in released} == {"probe complete"}
        refetched = await console.service.get_grant(context=context, grant_id=first.grant_id)
        assert refetched.status is KubernetesGrantStatus.RELEASED

    console.call(exercise)


def test_can_i_reports_standing_policy_through_the_configured_subject(console: _Console) -> None:
    context = console.agent_context()
    console.sar.allowed = True
    console.sar.reason = "RBAC: allowed by ClusterRoleBinding"

    async def exercise() -> None:
        (result,) = await console.service.can_i(context=context, requests=[KubernetesAccessCheck(attributes=_REQUEST)])
        assert result.allowed is True
        assert result.source is KubernetesAuthorizationSource.SAR
        assert result.valid_until is None

    console.call(exercise)
    # The review crossed the boundary as the profile-configured subject, never a caller-chosen one.
    assert console.sar.reviews == [(_SUBJECT, _REQUEST)]


def test_can_i_falls_back_to_an_active_grant_only_after_sar_denial(console: _Console) -> None:
    context = console.agent_context()

    async def exercise() -> None:
        (denied,) = await console.service.can_i(context=context, requests=[KubernetesAccessCheck(attributes=_REQUEST)])
        assert denied.allowed is False
        assert denied.source is KubernetesAuthorizationSource.SAR

        (grant,) = await console.service.create_grants(
            context=context, grants=[KubernetesGrantSpec(scope=_SCOPE, rules=(_RULE,))], duration_seconds=600
        )
        (allowed,) = await console.service.can_i(context=context, requests=[KubernetesAccessCheck(attributes=_REQUEST)])
        assert allowed.allowed is True
        assert allowed.source is KubernetesAuthorizationSource.GRANT
        assert allowed.valid_until == grant.expires_at

    console.call(exercise)


def test_can_i_infers_cluster_scope_for_builtin_cluster_scoped_kinds(console: _Console) -> None:
    """The natural probe — can_i on a built-in cluster-scoped kind with no declaration — is accepted."""
    context = console.agent_context()
    console.sar.allowed = True
    batch = [
        RequestAttributes(resource_request=True, verb="list", api_version="v1", resource="namespaces"),
        RequestAttributes(resource_request=True, verb="get", api_version="v1", resource="nodes", name="ovh-ns103656"),
        RequestAttributes(
            resource_request=True,
            verb="list",
            api_group="rbac.authorization.k8s.io",
            api_version="v1",
            resource="clusterroles",
        ),
        RequestAttributes(
            resource_request=True,
            verb="list",
            api_group="apiextensions.k8s.io",
            api_version="v1",
            resource="customresourcedefinitions",
        ),
        RequestAttributes(
            resource_request=True, verb="list", api_group="storage.k8s.io", api_version="v1", resource="storageclasses"
        ),
    ]

    async def exercise() -> None:
        results = await console.service.can_i(
            context=context, requests=[KubernetesAccessCheck(attributes=attributes) for attributes in batch]
        )
        assert [result.allowed for result in results] == [True] * len(batch)

    console.call(exercise)
    assert [attributes for _, attributes in console.sar.reviews] == batch


def test_can_i_inferred_cluster_scope_matches_cluster_grants(console: _Console) -> None:
    """The inferred scope is cluster: after SAR denial, a cluster-scoped grant satisfies the request."""
    context = console.agent_context()
    attributes = RequestAttributes(resource_request=True, verb="list", api_version="v1", resource="nodes")

    async def exercise() -> None:
        await console.service.create_grants(
            context=context,
            grants=[
                KubernetesGrantSpec(
                    scope=KubernetesClusterGrantScope(),
                    rules=(KubernetesRule(api_groups=("",), resources=("nodes",), verbs=("list",)),),
                )
            ],
            duration_seconds=600,
        )
        (allowed,) = await console.service.can_i(
            context=context, requests=[KubernetesAccessCheck(attributes=attributes)]
        )
        assert allowed.allowed is True
        assert allowed.source is KubernetesAuthorizationSource.GRANT

    console.call(exercise)


def test_can_i_explicit_unnamespaced_scope_declarations_still_work(console: _Console) -> None:
    context = console.agent_context()
    console.sar.allowed = True
    pods_all_namespaces = RequestAttributes(resource_request=True, verb="list", api_version="v1", resource="pods")
    crd_cluster = RequestAttributes(
        resource_request=True, verb="get", api_group="longhorn.io", api_version="v1beta2", resource="settings"
    )

    async def exercise() -> None:
        results = await console.service.can_i(
            context=context,
            requests=[
                KubernetesAccessCheck(
                    attributes=pods_all_namespaces, unnamespaced_resource_kind=KubernetesGrantScopeKind.ALL_NAMESPACES
                ),
                KubernetesAccessCheck(
                    attributes=crd_cluster, unnamespaced_resource_kind=KubernetesGrantScopeKind.CLUSTER
                ),
            ],
        )
        assert [result.allowed for result in results] == [True, True]

    console.call(exercise)
    assert [attributes for _, attributes in console.sar.reviews] == [pods_all_namespaces, crd_cluster]


def test_can_i_namespaced_builtin_without_declaration_still_rejects(console: _Console) -> None:
    """An unnamespaced ``pods`` request stays ambiguous — all namespaces or a forgotten namespace."""
    context = console.agent_context()
    check = KubernetesAccessCheck(
        attributes=RequestAttributes(resource_request=True, verb="list", api_version="v1", resource="pods")
    )

    async def exercise() -> None:
        with pytest.raises(ToolError, match=r"'pods'.*declare unnamespaced_resource_kind"):
            await console.service.can_i(context=context, requests=[check])

    console.call(exercise)
    assert console.sar.reviews == []


def test_can_i_unknown_unnamespaced_kind_rejects_with_one_line_tool_error(console: _Console) -> None:
    """A kind outside the built-in set still must declare its scope; the caller sees one clean line.

    ``nodes`` in group ``longhorn.io`` also pins that inference matches on (api_group, resource):
    the core ``nodes`` kind is cluster-scoped while Longhorn's is namespaced.
    """
    context = console.agent_context()
    check = {
        "attributes": {
            "resource_request": True,
            "verb": "list",
            "api_group": "longhorn.io",
            "api_version": "v1beta2",
            "resource": "nodes",
        }
    }

    async def exercise() -> str:
        async with Client(build_mcp(console.service)) as client:
            result = await client.call_tool(
                "can_i", {"requests": [check]}, meta=mcp_execution_request_meta(context), raise_on_error=False
            )
        assert result.is_error
        block = result.content[0]
        assert isinstance(block, TextContent)
        return block.text

    message = console.call(exercise)
    assert "\n" not in message
    assert "requests[0]" in message
    assert "'nodes.longhorn.io'" in message
    assert "unnamespaced_resource_kind" in message
    assert "'all_namespaces' or 'cluster'" in message
    assert console.sar.reviews == []


def test_can_i_rejects_a_declaration_that_is_not_an_unnamespaced_scope(console: _Console) -> None:
    """A nonsense declaration fails loudly instead of being overridden by built-in inference."""
    context = console.agent_context()
    check = KubernetesAccessCheck(
        attributes=RequestAttributes(resource_request=True, verb="list", api_version="v1", resource="namespaces"),
        unnamespaced_resource_kind=KubernetesGrantScopeKind.NAMESPACES,
    )

    async def exercise() -> None:
        with pytest.raises(ToolError, match=r"requests\[0\].*'all_namespaces' or 'cluster'"):
            await console.service.can_i(context=context, requests=[check])

    console.call(exercise)


if __name__ == "__main__":
    pytest_bazel.main()
