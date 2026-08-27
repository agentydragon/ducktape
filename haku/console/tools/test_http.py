"""Integration tests for the HTTP grant MCP tools over the real grant store.

The tools service runs against the app's PostgreSQL-backed grant service, so every assertion
observes durable state — created rows, source provenance, principal applicability — rather than
call forwarding.
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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.conftest import default_agent_binding, insert_approved_tool_call, insert_live_session
from haku.console.grant_principal import (
    AgentGrantPrincipal,
    GrantPrincipalKind,
    RequestPrincipal,
    SessionGrantPrincipal,
)
from haku.console.http_grant_models import HttpGrantStatus, HttpOrigin, HttpScheme
from haku.console.http_grant_service import HttpGrantService
from haku.console.mcp_execution import AgentMcpExecutionCaller, McpExecutionContext, OperatorMcpExecutionCaller
from haku.console.tools.http import HttpToolsService, build_mcp

_NOW = datetime(2026, 8, 27, tzinfo=UTC)
_ORIGIN = HttpOrigin(scheme=HttpScheme.HTTPS, host="grocy.example", port=443)
_OTHER_ORIGIN = HttpOrigin(scheme=HttpScheme.HTTP, host="mirror.example", port=80)
# The access profile `make_client` assigns its seeded default static agent.
_PROFILE = "no_auto_approval"


@dataclass(frozen=True, slots=True)
class _Console:
    """One console app over a fresh migrated database, its grant store wired into the tools."""

    client: TestClient
    sessions: async_sessionmaker[AsyncSession]
    service: HttpToolsService
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
                server_id="http",
                session_id=session_id,
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
        grants = cast(HttpGrantService, app.state.http_grants)
        assert client.portal is not None
        agent_id, binding_id = client.portal.call(default_agent_binding, sessions)
        yield _Console(
            client=client,
            sessions=sessions,
            service=HttpToolsService(grants=grants),
            agent_id=agent_id,
            binding_id=binding_id,
        )


def test_server_exposes_exact_stable_tool_set_without_context_argument(console: _Console) -> None:
    async def list_tools() -> list[Any]:
        async with Client(build_mcp(console.service)) as client:
            return list(await client.list_tools())

    tools = console.call(list_tools)
    assert {tool.name for tool in tools} == {"create_grant", "list_grants", "get_grant", "release_grants"}
    for tool in tools:
        assert "context" not in tool.inputSchema.get("properties", {})
    create_grant = next(tool for tool in tools if tool.name == "create_grant")
    assert set(create_grant.inputSchema["properties"]) == {"origins", "duration_seconds", "applies_to"}
    assert create_grant.inputSchema["properties"]["applies_to"]["default"] == "agent"
    assert create_grant.inputSchema["properties"]["origins"]["minItems"] == 1
    assert create_grant.inputSchema["properties"]["origins"]["maxItems"] == 32
    release_grants = next(tool for tool in tools if tool.name == "release_grants")
    assert set(release_grants.inputSchema["properties"]) == {"grant_ids", "reason"}
    assert release_grants.inputSchema["properties"]["grant_ids"]["minItems"] == 1
    assert release_grants.inputSchema["properties"]["grant_ids"]["maxItems"] == 32


def test_create_persists_trusted_identity_provenance_and_exact_origins(console: _Console) -> None:
    context = console.agent_context()
    requested = [_ORIGIN, _OTHER_ORIGIN]

    async def exercise() -> None:
        created = await console.service.create_grants(context=context, origins=requested, duration_seconds=600)
        assert [grant.origin for grant in created] == requested
        for grant in created:
            assert grant.owner_agent_id == console.agent_id
            assert grant.principal == AgentGrantPrincipal(agent_id=console.agent_id)
            assert grant.source_tool_call_id == context.tool_call_id
            assert grant.status is HttpGrantStatus.ACTIVE
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
            lambda service: service.create_grants(context=_operator_context(), origins=[_ORIGIN], duration_seconds=60),
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
    console: _Console, operation: Callable[[HttpToolsService], Awaitable[object]]
) -> None:
    with pytest.raises(PermissionError):
        console.call(partial(operation, console.service))


def test_session_scope_binds_the_grant_to_the_exact_live_session(console: _Console) -> None:
    session_id = console.live_session()
    session_context = console.agent_context(session_id=session_id)
    static_context = console.agent_context()

    async def exercise() -> None:
        (session_grant,) = await console.service.create_grants(
            context=session_context, origins=[_ORIGIN], duration_seconds=600, applies_to=GrantPrincipalKind.SESSION
        )
        assert session_grant.principal == SessionGrantPrincipal(session_id=session_id)
        (agent_grant,) = await console.service.create_grants(
            context=static_context, origins=[_ORIGIN], duration_seconds=600
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
                context=context, origins=[_ORIGIN], duration_seconds=600, applies_to=GrantPrincipalKind.SESSION
            )
        assert await console.service.list_grants(context=context) == ()

    console.call(exercise)


def test_release_ends_grants_in_the_supplied_order(console: _Console) -> None:
    context = console.agent_context()

    async def exercise() -> None:
        first, second = await console.service.create_grants(
            context=context, origins=[_ORIGIN, _OTHER_ORIGIN], duration_seconds=600
        )
        released = await console.service.release_grants(
            context=context, grant_ids=[second.grant_id, first.grant_id], reason="probe complete"
        )
        assert [grant.grant_id for grant in released] == [second.grant_id, first.grant_id]
        assert all(grant.status is HttpGrantStatus.RELEASED for grant in released)
        assert {grant.end_reason for grant in released} == {"probe complete"}
        refetched = await console.service.get_grant(context=context, grant_id=first.grant_id)
        assert refetched.status is HttpGrantStatus.RELEASED

    console.call(exercise)


def test_grants_admit_the_matcher_only_for_the_exact_origin_and_principal(console: _Console) -> None:
    """End to end through the real store: what create_grant mints is what match_request honors."""

    context = console.agent_context()

    async def exercise() -> None:
        (grant,) = await console.service.create_grants(context=context, origins=[_ORIGIN], duration_seconds=600)
        principal = context.request_principal
        allowed = await console.service.grants.match_request(request_principal=principal, origin=_ORIGIN)
        assert allowed.allowed
        assert allowed.grant_id == grant.grant_id
        assert allowed.expires_at == grant.expires_at
        assert not (
            await console.service.grants.match_request(request_principal=principal, origin=_OTHER_ORIGIN)
        ).allowed
        assert not (
            await console.service.grants.match_request(
                request_principal=RequestPrincipal(agent_id=UUID(int=7), session_id=None, access_profile_id=None),
                origin=_ORIGIN,
            )
        ).allowed

        await console.service.release_grants(context=context, grant_ids=[grant.grant_id], reason="done")
        assert not (await console.service.grants.match_request(request_principal=principal, origin=_ORIGIN)).allowed

    console.call(exercise)


if __name__ == "__main__":
    pytest_bazel.main()
