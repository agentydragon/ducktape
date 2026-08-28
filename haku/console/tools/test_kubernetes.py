"""Integration tests for the Kubernetes ``can_i`` access-check service over the real grant store.

The service answers the ``grants`` server's ``kubernetes_can_i`` tool (#4918); here it is exercised
directly, so the SAR→grant fallback observes durable state — active grants created directly in the
store — rather than call forwarding. The only stand-in is the SubjectAccessReview client: the
in-cluster Kubernetes API is a genuine external boundary. The tool's wiring onto the ``grants``
server, and grant lifecycle (create/list/…), are exercised in ``test_grants.py``; here the store is
seeded only to prove ``can_i`` reflects active grants.
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
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.agent_bearer_authority import AgentBearerAuthority
from haku.console.config import KubernetesAuthorizationConfig, KubernetesAuthorizationSubject
from haku.console.conftest import (
    # TestClient drives the app over httpx, imported inside starlette; gazelle cannot see it.
    # gazelle:include_dep @pypi//httpx
    DEFAULT_ACCESS_PROFILE_ID,
    default_agent_binding,
    insert_approved_tool_call,
)
from haku.console.grants.kubernetes.authorization import (
    KubernetesAuthorizationService,
    KubernetesAuthorizationSource,
    RequestAttributes,
    SubjectAccessReviewResult,
)
from haku.console.grants.kubernetes.models import (
    ClusterGrantScope,
    GrantScopeKind,
    GrantSpec,
    NamespacesGrantScope,
    Rule,
)
from haku.console.grants.kubernetes.service import GrantService
from haku.console.grants.principal import AgentGrantPrincipal, RequestPrincipal
from haku.console.mcp_execution import AgentMcpExecutionCaller, McpExecutionContext
from haku.console.tools.kubernetes import KubernetesAccessCheck, KubernetesToolsService

_NOW = datetime(2026, 8, 20, tzinfo=UTC)
_SCOPE = NamespacesGrantScope(namespaces=("demo",))
_RULE = Rule(api_groups=("",), resources=("pods",), verbs=("get",))
_REQUEST = RequestAttributes(
    resource_request=True,
    verb="get",
    api_version="v1",
    namespace="demo",
    resource="pods",
    path="/api/v1/namespaces/demo/pods",
)
_SUBJECT = KubernetesAuthorizationSubject(username="haku-agent-subject", groups=("haku-agents",))


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
    """One console app over a fresh migrated database, its grant store wired into the tool."""

    client: TestClient
    sessions: async_sessionmaker[AsyncSession]
    service: KubernetesToolsService
    grants: GrantService
    sar: _FakeSubjectAccessReviews
    agent_id: UUID
    binding_id: UUID

    def call[T](self, func: Callable[..., Awaitable[T]], *args: Any) -> T:
        """Run one async step on the app's own event loop, where its engine lives."""
        assert self.client.portal is not None
        return self.client.portal.call(func, *args)

    def agent_context(self) -> McpExecutionContext:
        """A trusted Agent execution whose fresh ToolCall satisfies grant source provenance."""
        tool_call_id = self.call(
            partial(insert_approved_tool_call, self.sessions, binding_id=self.binding_id, now=_NOW)
        )
        return McpExecutionContext(
            caller=AgentMcpExecutionCaller(
                principal=RequestPrincipal(
                    agent_id=self.agent_id, session_id=None, access_profile_id=DEFAULT_ACCESS_PROFILE_ID
                )
            ),
            tool_call_id=tool_call_id,
            approving_operator_id=None,
            approval_policy_id=None,
        )

    async def seed_grant(self, context: McpExecutionContext, spec: GrantSpec) -> datetime:
        """Create one active Agent grant directly in the store, as the SAR-fallback target."""
        assert context.tool_call_id is not None
        (grant,) = await self.grants.create_grants(
            owner_agent_id=self.agent_id,
            grant_principal=AgentGrantPrincipal(agent_id=self.agent_id),
            source_tool_call_id=context.tool_call_id,
            grants=[spec],
            expires_at=datetime.now(UTC) + timedelta(seconds=600),
        )
        return grant.expires_at


@pytest.fixture
def console(make_client: Callable[..., Any]) -> Iterator[_Console]:
    with make_client() as client:
        app = cast(FastAPI, client.app)
        sessions = cast(async_sessionmaker[AsyncSession], app.state.db_sessions)
        grants = cast(GrantService, app.state.kubernetes_grants)
        sar = _FakeSubjectAccessReviews()
        authorization = KubernetesAuthorizationService(
            config=KubernetesAuthorizationConfig(subjects_by_access_profile={DEFAULT_ACCESS_PROFILE_ID: _SUBJECT}),
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
            service=KubernetesToolsService(authorization=authorization),
            grants=grants,
            sar=sar,
            agent_id=agent_id,
            binding_id=binding_id,
        )


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

        expires_at = await console.seed_grant(context, GrantSpec(scope=_SCOPE, rules=(_RULE,)))
        (allowed,) = await console.service.can_i(context=context, requests=[KubernetesAccessCheck(attributes=_REQUEST)])
        assert allowed.allowed is True
        assert allowed.source is KubernetesAuthorizationSource.GRANT
        assert allowed.valid_until == expires_at

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
        await console.seed_grant(
            context,
            GrantSpec(
                scope=ClusterGrantScope(), rules=(Rule(api_groups=("",), resources=("nodes",), verbs=("list",)),)
            ),
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
                    attributes=pods_all_namespaces, unnamespaced_resource_kind=GrantScopeKind.ALL_NAMESPACES
                ),
                KubernetesAccessCheck(attributes=crd_cluster, unnamespaced_resource_kind=GrantScopeKind.CLUSTER),
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
    """A kind outside the built-in set still must declare its scope; the service raises one clean
    ``ToolError`` line (FastMCP renders it as a single message — covered end to end for the tool in
    ``test_grants.py``), not a multi-line pydantic trace.

    ``nodes`` in group ``longhorn.io`` also pins that inference matches on (api_group, resource):
    the core ``nodes`` kind is cluster-scoped while Longhorn's is namespaced.
    """
    context = console.agent_context()
    check = KubernetesAccessCheck(
        attributes=RequestAttributes(
            resource_request=True, verb="list", api_group="longhorn.io", api_version="v1beta2", resource="nodes"
        )
    )

    async def exercise() -> None:
        with pytest.raises(ToolError) as error:
            await console.service.can_i(context=context, requests=[check])
        message = str(error.value)
        assert "\n" not in message
        assert "requests[0]" in message
        assert "'nodes.longhorn.io'" in message
        assert "unnamespaced_resource_kind" in message
        assert "'all_namespaces' or 'cluster'" in message

    console.call(exercise)
    assert console.sar.reviews == []


def test_can_i_rejects_a_declaration_that_is_not_an_unnamespaced_scope(console: _Console) -> None:
    """A nonsense declaration fails loudly instead of being overridden by built-in inference."""
    context = console.agent_context()
    check = KubernetesAccessCheck(
        attributes=RequestAttributes(resource_request=True, verb="list", api_version="v1", resource="namespaces"),
        unnamespaced_resource_kind=GrantScopeKind.NAMESPACES,
    )

    async def exercise() -> None:
        with pytest.raises(ToolError, match=r"requests\[0\].*'all_namespaces' or 'cluster'"):
            await console.service.can_i(context=context, requests=[check])

    console.call(exercise)


if __name__ == "__main__":
    pytest_bazel.main()
