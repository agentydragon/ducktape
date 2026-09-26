"""Configured Actions as direct MCP tools of an external Connection, through the real MCP frontend.

The bearer is the one external boundary faked here: `ConnectionBearers` resolves a test token to a real,
activated Connection grant exactly as the OAuth adapter would resolve an issued one
(`test_oauth.py` covers that half).
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx2
import pytest
import pytest_bazel
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError
from mcp.types import CallToolResult, ContentBlock, ImageContent, TextContent, ToolAnnotations
from more_itertools import one
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.applications import Starlette
from starlette.routing import Route

from agentplane.action_service.caller_auth import CallerToken, CallerTokenVerifier
from agentplane.action_service.catalog import (
    ActionCatalog,
    ActionDefinition,
    ActionGroup,
    ActionIdentity,
    McpExecutorBinding,
)
from agentplane.action_service.conftest import ScriptedExecutor, lifespan_in_own_task
from agentplane.action_service.connections import ConnectionAuthority, Grant, GrantBinding, NewConnection
from agentplane.action_service.db import ActionStore, make_sessionmaker
from agentplane.action_service.direct_tools import DIRECT_CALL_TITLE, DIRECT_WAIT_SECONDS
from agentplane.action_service.github_policy.visibility import RepositoryVisibilityService
from agentplane.action_service.mcp_frontend import TransportDisconnects, create_server
from agentplane.action_service.models import ActionState, CallerPrincipal, ExecutionResult, ExecutionState
from agentplane.action_service.policies.resources import parse_binding, parse_policy_set
from agentplane.action_service.policy_evaluation import PROVIDER_NAME, PolicySetDecisionProvider
from agentplane.action_service.policy_informer import PolicyIndex
from agentplane.action_service.sandbox.binding import SandboxExecutorBinding
from agentplane.action_service.sandbox.models import ExecResult
from agentplane.action_service.service import ActionService
from agentplane.action_service.test_fixtures.callers import OTHER, PERSONAL, TEST_NAMESPACE, admitted_callers
from agentplane.action_service.updates import ActionUpdates
from agentplane.workload_auth.principal import WorkloadPrincipalResolver
from mcp_infra.exec.models import Exited

EXTERNAL_TOKEN = "test-external-token"
WORKLOAD_TOKEN = "test-workload-token"
SNAPSHOT = ActionIdentity(group="test-mcp", name="snapshot")
EXEC = ActionIdentity(group="test-sandbox", name="exec")


def _catalog() -> ActionCatalog:
    def action(title: str | None = None, annotations: ToolAnnotations | None = None) -> ActionDefinition:
        return ActionDefinition(
            description=f"test-{title}-description",
            input_schema={"type": "object"},
            title=title,
            annotations=annotations,
        )

    return ActionCatalog(
        groups={
            "test-mcp": ActionGroup(
                title="Test MCP",
                description="test-mcp-description",
                executor=McpExecutorBinding(description="test-mcp-executor"),
                actions={
                    "snapshot": action("Take snapshot", ToolAnnotations(read_only_hint=True)),
                    "search": action("Search"),
                    "unapproved": action("Unapproved"),
                    "internal": action("Internal"),
                },
                # `internal` is approvable but not configured; `unapproved` is configured but no policy names it.
                direct_tools=frozenset({"snapshot", "search", "unapproved"}),
            ),
            "test-sandbox": ActionGroup(
                title="Test sandbox",
                description="test-sandbox-description",
                executor=SandboxExecutorBinding(
                    description="test-sandbox-executor", namespace=TEST_NAMESPACE, templates={"test-template"}
                ),
                actions={"exec": action("Run script")},
                direct_tools=frozenset({"exec"}),
            ),
        }
    )


def _policies() -> PolicyIndex:
    """PERSONAL may run `snapshot`, `internal` and `exec` outright, and `search` only for public
    targets; OTHER is bound to nothing."""
    index = admitted_callers(PERSONAL, OTHER)
    metadata = {"namespace": TEST_NAMESPACE, "generation": 1, "resourceVersion": "1"}
    policy_set = parse_policy_set(
        {
            "metadata": {"name": "test-direct", "uid": "uid-test-direct", **metadata},
            "spec": {
                "autoApproveIf": [
                    {
                        "type": "exact_actions",
                        "actions": {"test-mcp": ["snapshot", "internal"], "test-sandbox": ["exec"]},
                    },
                    {
                        "type": "argument_schema",
                        "actions": {"test-mcp": ["search"]},
                        "schema": {"type": "object", "required": ["public"], "properties": {"public": {"const": True}}},
                    },
                ]
            },
        }
    )
    index.policy_sets[policy_set.namespaced_name] = policy_set
    binding = parse_binding(
        {
            "metadata": {"name": "test-personal-direct", "uid": "uid-test-personal-direct", **metadata},
            "spec": {"subject": {"namespace": TEST_NAMESPACE, "name": PERSONAL.name}, "policySets": ["test-direct"]},
        }
    )
    index.bindings[binding.namespaced_name] = binding
    return index


class ConnectionBearers(CallerTokenVerifier):
    """The external token resolves to the grant, the workload token to OTHER's own account."""

    def __init__(self, grant: Grant, callers: PolicyIndex) -> None:
        super().__init__(AsyncMock(spec=WorkloadPrincipalResolver), callers=callers, oauth=None)
        self._grant = grant

    async def verify_token(self, token: str) -> CallerToken | None:
        if token == EXTERNAL_TOKEN:
            provenance = self._grant.provenance()
            return CallerToken(
                token=token,
                client_id=provenance.client_id,
                scopes=[],
                principal=self._grant.principal(),
                external_grant=provenance,
            )
        if token == WORKLOAD_TOKEN:
            return CallerToken(
                token=token,
                client_id="test-workload",
                scopes=[],
                principal=CallerPrincipal(account=OTHER),
                external_grant=None,
            )
        return None


@dataclass
class Direct:
    app: Starlette
    store: ActionStore
    grant: Grant

    def client(self, token: str) -> Client[StreamableHttpTransport]:
        def factory(
            headers: dict[str, str] | None = None,
            timeout: httpx2.Timeout | None = None,
            auth: httpx2.Auth | None = None,
            *,
            follow_redirects: bool = True,
        ) -> httpx2.AsyncClient:
            return httpx2.AsyncClient(
                transport=httpx2.ASGITransport(self.app),
                headers=headers,
                timeout=timeout or httpx2.Timeout(60),
                auth=auth,
                follow_redirects=follow_redirects,
            )

        return Client(
            StreamableHttpTransport(
                "http://actions.test/mcp", headers={"Authorization": f"Bearer {token}"}, httpx_client_factory=factory
            )
        )


@pytest.fixture
def direct_wait_seconds() -> float:
    return DIRECT_WAIT_SECONDS


@pytest.fixture
async def direct(
    engine: AsyncEngine,
    db_url: str,
    scripted: ScriptedExecutor,
    github_visibility: Callable[..., RepositoryVisibilityService],
    direct_wait_seconds: float,
) -> AsyncIterator[Direct]:
    policies = _policies()
    authority = ConnectionAuthority(make_sessionmaker(engine), policies)
    bound = await authority.bind(
        GrantBinding(
            grant_id=uuid4(),
            service_account=PERSONAL,
            issuer="https://test-issuer.example.test",
            client_id="test-client",
            activation_deadline=datetime.now(UTC) + timedelta(minutes=10),
            connection=NewConnection(display_name="test-connection"),
        )
    )
    grant = await authority.activate(bound.id)
    store = ActionStore(make_sessionmaker(engine), external_grants=authority)
    catalog = _catalog()
    service = ActionService(
        store,
        catalog,
        dict.fromkeys(catalog.groups, scripted),
        providers=[PolicySetDecisionProvider(visibility=github_visibility())],
        policies=policies,
    )
    updates = ActionUpdates(db_url)
    mcp_app = create_server(
        service, catalog, updates, ConnectionBearers(grant, policies), direct_wait_seconds=direct_wait_seconds
    ).http_app(path="/mcp", stateless_http=True, json_response=False)

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        await updates.start()
        try:
            async with mcp_app.lifespan(mcp_app):
                yield
        finally:
            await updates.close()

    app = Starlette(routes=[Route("/mcp", TransportDisconnects(mcp_app))], lifespan=lifespan)
    try:
        async with lifespan_in_own_task(app):
            yield Direct(app, store, grant)
    finally:
        await service.close()


def _text(content: list[ContentBlock]) -> str:
    return one(block.text for block in content if isinstance(block, TextContent))


async def test_external_caller_sees_configured_tools_its_policy_can_approve(direct: Direct) -> None:
    async with direct.client(EXTERNAL_TOKEN) as external, direct.client(WORKLOAD_TOKEN) as workload:
        tools = {tool.name: tool for tool in await external.list_tools()}
        workload_tools = {tool.name for tool in await workload.list_tools()}
        with pytest.raises(ToolError):
            await workload.call_tool("test-mcp__snapshot", {})
    assert {name for name in tools if "__" in name} == {"test-mcp__snapshot", "test-mcp__search", "test-sandbox__exec"}
    snapshot = tools["test-mcp__snapshot"]
    assert snapshot.title == "Test MCP: Take snapshot"
    assert snapshot.annotations == ToolAnnotations(read_only_hint=True)
    assert snapshot.input_schema == {"type": "object"}
    # A workload's own surface is unchanged: the generic tools only.
    assert not any("__" in name for name in workload_tools)


async def test_direct_call_runs_the_action_and_answers_as_its_tool_did(
    direct: Direct, scripted: ScriptedExecutor
) -> None:
    answer = CallToolResult(
        content=[ImageContent(type="image", data=base64.b64encode(b"test-image").decode(), mime_type="image/png")],
        structured_content={"width": 1},
    )
    scripted.results[SNAPSHOT] = ExecutionResult(
        state=ExecutionState.SUCCEEDED, result=answer.model_dump(mode="json", by_alias=True, exclude_none=True)
    )
    ran = ExecResult(exit=Exited(exit_code=0), stdout="test-output", stderr="", duration_seconds=0.1)
    scripted.results[EXEC] = ExecutionResult(state=ExecutionState.SUCCEEDED, result=ran.model_dump(mode="json"))
    async with direct.client(EXTERNAL_TOKEN) as client:
        snapshot = await client.call_tool("test-mcp__snapshot", {})
        executed = await client.call_tool("test-sandbox__exec", {})
    assert snapshot.content == answer.content
    assert snapshot.structured_content == {"width": 1}
    assert ExecResult.model_validate(executed.structured_content) == ran
    # Each call is an ordinary Action: auto-approved by policy, with the Connection's provenance.
    requests = await direct.store.list_requests(direct.grant.principal())
    assert {request.action for request in requests} == {SNAPSHOT, EXEC}
    for request in requests:
        assert request.state is ActionState.SUCCEEDED
        assert request.title == DIRECT_CALL_TITLE
        assert request.external_grant == direct.grant.provenance()
        assert request.decision is not None
        assert request.decision.provider == PROVIDER_NAME


@pytest.mark.parametrize("direct_wait_seconds", [0.5])
async def test_direct_call_still_running_after_its_wait_answers_with_its_request(
    direct: Direct, scripted: ScriptedExecutor
) -> None:
    ran = ExecResult(exit=Exited(exit_code=0), stdout="test-late-output", stderr="", duration_seconds=0.1)
    scripted.results[EXEC] = ExecutionResult(state=ExecutionState.SUCCEEDED, result=ran.model_dump(mode="json"))
    scripted.release.clear()
    async with direct.client(EXTERNAL_TOKEN) as client:
        pending = await client.call_tool("test-sandbox__exec", {})
        scripted.release.set()
        assert pending.structured_content is not None
        finished = await client.call_tool(
            "get_action_result", {"request_id": pending.structured_content["request_id"], "wait_seconds": 10}
        )
    # The call answered while its Action was approved and still running, and the Action went on to finish.
    assert not pending.is_error
    assert ActionState(pending.structured_content["state"]) in {
        ActionState.ALLOWED,
        ActionState.DISPATCHING,
        ActionState.RUNNING,
    }
    assert ExecResult.model_validate(finished.structured_content) == ran


async def test_call_no_policy_approves_is_refused_with_its_reason_and_leaves_nothing(direct: Direct) -> None:
    async with direct.client(EXTERNAL_TOKEN) as client:
        unlisted = await client.call_tool("test-mcp__unapproved", {}, raise_on_error=False)
        private = await client.call_tool("test-mcp__search", {"public": False}, raise_on_error=False)
    assert unlisted.is_error
    assert "lists test-mcp/unapproved" in _text(unlisted.content)
    assert private.is_error
    assert "argument_schema" in _text(private.content)
    assert "request_action" in _text(private.content)
    assert await direct.store.list_requests(direct.grant.principal()) == []


if __name__ == "__main__":
    pytest_bazel.main()
