"""Operator browser API for inspecting Kubernetes grants."""

from __future__ import annotations

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from haku.console.grants.catalog import DatabaseGrantSource, Grant, GrantCatalog
from haku.console.identity.operator_agents import AgentEnrollmentServiceDep, owned_agents
from haku.console.identity.operator_auth import OperatorActorDep

router = APIRouter(prefix="/api/kubernetes-grants", tags=["kubernetes-grants"])


class OperatorGrant(BaseModel):
    """One grant plus the operator-owned Agent name used by the browser."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    grant: Grant
    agent_id: UUID
    agent_display_name: str


class GrantListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    grants: tuple[OperatorGrant, ...]


def _grant_catalog(request: Request) -> GrantCatalog:
    return cast(GrantCatalog, request.app.state.grant_catalog)


GrantCatalogDep = Annotated[GrantCatalog, Depends(_grant_catalog)]


@router.get("", response_model=GrantListResponse)
async def list_kubernetes_grants(
    actor: OperatorActorDep, catalog: GrantCatalogDep, agents: AgentEnrollmentServiceDep
) -> GrantListResponse:
    """List configuration and database authority for only this Operator's Agents."""

    owned = await owned_agents(actor=actor, agents=agents)
    records = [
        OperatorGrant(grant=grant, agent_id=agent.agent_id, agent_display_name=display_name)
        for agent in owned
        for grant in await catalog.list_kubernetes_for_agent(
            agent_id=agent.agent_id, access_profile_id=agent.access_profile_id
        )
        for display_name in (agent.display_name,)
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
