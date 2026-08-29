"""The manual-approval provenance invariant every grant domain enforces at creation.

A grant's ``source_tool_call_id`` must identify a manually approved ToolCall authenticated
by the lifecycle owner, and the grant principal must match that durable source principal —
an Agent's ToolCall can never author a principal wider than itself (#4670's non-goal, as a
query). Split from :mod:`haku.console.grants.envelope` because these checks read
``database_schema`` rows, which ``database_schema`` itself imports the envelope's column
mixin from.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from haku.console.database_schema import Agent, CredentialBinding, McpToolCall, McpToolCallPrincipal, Session
from haku.console.grants.envelope import GrantNotFoundError, GrantOwnershipError, GrantSourceError
from haku.console.grants.principal import AccessProfileGrantPrincipal, AgentGrantPrincipal, GrantPrincipal
from haku.console.identity.agent import AgentStatus
from haku.console.session.status import SessionStatus
from haku.console.tool_calls import ToolCallStatus


@dataclass(frozen=True, slots=True)
class SourceToolFilter:
    """Pin the source ToolCall to one exact grant-creation tool."""

    server_id: str
    tool_name: str


async def assert_owner_principal_and_source(
    session: AsyncSession,
    *,
    owner_agent_id: UUID,
    grant_principal: GrantPrincipal,
    source_tool_call_id: str,
    source_tool: SourceToolFilter | None,
) -> None:
    """Validate owner eligibility, source-ToolCall provenance, and principal applicability.

    Locks the source ToolCall row, serializing grant-set creation against concurrent
    replays and revocations of the same source. An exact-session principal additionally
    requires the named session to be live, bound to the source call's credential binding,
    and within its lease.
    """

    agent = await session.scalar(select(Agent).where(Agent.agent_id == owner_agent_id))
    if agent is None or agent.status in (AgentStatus.ABANDONED, AgentStatus.DELETED):
        raise GrantOwnershipError(f"Agent {owner_agent_id} is not eligible to own a grant")
    source_filters = (
        (McpToolCall.server_id == source_tool.server_id, McpToolCall.tool_name == source_tool.tool_name)
        if source_tool is not None
        else ()
    )
    source = await session.scalar(
        select(McpToolCallPrincipal)
        .join(CredentialBinding, CredentialBinding.binding_id == McpToolCallPrincipal.binding_id)
        .join(McpToolCall, McpToolCall.tool_call_id == McpToolCallPrincipal.tool_call_id)
        .where(
            McpToolCallPrincipal.tool_call_id == source_tool_call_id,
            CredentialBinding.agent_id == owner_agent_id,
            *source_filters,
            or_(McpToolCall.status == ToolCallStatus.RUNNING, McpToolCall.status == ToolCallStatus.OK),
            McpToolCall.approved_at.is_not(None),
            McpToolCall.approval_policy_id.is_(None),
        )
        .with_for_update(of=McpToolCall)
    )
    if source is None:
        described = f"{source_tool.server_id}/{source_tool.tool_name} " if source_tool is not None else ""
        raise GrantSourceError(
            f"source_tool_call_id must identify a manually approved {described}call "
            "authenticated by the lifecycle owner"
        )
    if isinstance(grant_principal, AgentGrantPrincipal):
        valid_principal = grant_principal.agent_id == owner_agent_id
    elif isinstance(grant_principal, AccessProfileGrantPrincipal):
        valid_principal = grant_principal.access_profile_id == agent.access_profile_id
    else:
        valid_principal = source.session_id is not None and grant_principal.session_id == source.session_id
        if valid_principal:
            live_session = await session.scalar(
                select(Session)
                .where(
                    Session.session_id == grant_principal.session_id,
                    Session.agent_binding_id == source.binding_id,
                    Session.status == SessionStatus.READY,
                    Session.lease_expires_at > datetime.datetime.now(datetime.UTC),
                )
                .with_for_update()
            )
            valid_principal = live_session is not None
    if not valid_principal:
        raise GrantSourceError("grant principal does not match the durable source ToolCall principal")


async def lock_owned_source(session: AsyncSession, *, owner_agent_id: UUID, source_tool_call_id: str) -> None:
    """Lock the owner's durable source ToolCall, serializing grant-set lifecycle operations."""

    source = await session.scalar(
        select(McpToolCall)
        .join(McpToolCallPrincipal, McpToolCallPrincipal.tool_call_id == McpToolCall.tool_call_id)
        .join(CredentialBinding, CredentialBinding.binding_id == McpToolCallPrincipal.binding_id)
        .where(McpToolCall.tool_call_id == source_tool_call_id, CredentialBinding.agent_id == owner_agent_id)
        .with_for_update(of=McpToolCall)
    )
    if source is None:
        raise GrantNotFoundError(source_tool_call_id)
