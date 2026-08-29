"""Operator browser API for inspecting HTTP egress grants."""

from __future__ import annotations

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from haku.console.grants.catalog import DatabaseGrantSource, Grant, GrantCatalog
from haku.console.identity.operator_agents import AgentEnrollmentServiceDep, owned_agents
from haku.console.identity.operator_auth import OperatorActorDep

router = APIRouter(prefix="/api/http-grants", tags=["http-grants"])


class AgentGrant(BaseModel):
    """One grant and the Agent that owns it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    grant: Grant
    agent_id: UUID


class GrantListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    grants: tuple[AgentGrant, ...]


def _grant_catalog(request: Request) -> GrantCatalog:
    return cast(GrantCatalog, request.app.state.grant_catalog)


GrantCatalogDep = Annotated[GrantCatalog, Depends(_grant_catalog)]


@router.get("", response_model=GrantListResponse)
async def list_http_grants(
    actor: OperatorActorDep, catalog: GrantCatalogDep, agents: AgentEnrollmentServiceDep
) -> GrantListResponse:
    """List configuration and database authority for only this Operator's Agents."""

    owned = await owned_agents(actor=actor, agents=agents)
    records = [
        AgentGrant(grant=grant, agent_id=agent.agent_id)
        for agent in owned
        for grant in await catalog.list_http_for_agent(
            agent_id=agent.agent_id, access_profile_id=agent.access_profile_id
        )
    ]
    records.sort(
        key=lambda item: (
            isinstance(item.grant.source, DatabaseGrantSource),
            item.grant.source.created_at if isinstance(item.grant.source, DatabaseGrantSource) else None,
            str(item.grant.source.id)
            if isinstance(item.grant.source, DatabaseGrantSource)
            else item.grant.source.entry_id,
        ),
        reverse=True,
    )
    return GrantListResponse(grants=tuple(records))
