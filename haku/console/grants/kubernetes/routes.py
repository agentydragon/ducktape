"""Operator browser API for inspecting and revoking temporary Kubernetes grants."""

from __future__ import annotations

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from haku.console.grants.catalog import DatabaseGrantSource, Grant, GrantCatalog
from haku.console.grants.envelope import GrantNotFoundError, GrantOwnershipError
from haku.console.grants.kubernetes.service import GrantService
from haku.console.identity.operator_agents import AgentEnrollmentServiceDep, owned_agent_names, owned_agents
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


class RevokeGrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def reason_must_not_be_blank(cls, value: str) -> str:
        reason = value.strip()
        if not reason:
            raise ValueError("revocation reason must not be blank")
        return reason


def _grant_service(request: Request) -> GrantService:
    return cast(GrantService, request.app.state.kubernetes_grants)


GrantServiceDep = Annotated[GrantService, Depends(_grant_service)]


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


@router.post("/{agent_id}/{grant_id}/revoke", response_model=OperatorGrant)
async def revoke_kubernetes_grant(
    agent_id: UUID,
    grant_id: UUID,
    body: RevokeGrantRequest,
    actor: OperatorActorDep,
    grants: GrantServiceDep,
    catalog: GrantCatalogDep,
    agents: AgentEnrollmentServiceDep,
) -> OperatorGrant:
    """Revoke one owned Agent's grant with a durable, operator-supplied reason."""

    owned = await owned_agent_names(actor=actor, agents=agents)
    display_name = owned.get(agent_id)
    if display_name is None:
        raise HTTPException(status_code=404, detail="Kubernetes grant not found")
    try:
        grant = await grants.revoke_grant(owner_agent_id=agent_id, grant_id=grant_id, reason=body.reason)
    except (GrantNotFoundError, GrantOwnershipError) as error:
        raise HTTPException(status_code=404, detail="Kubernetes grant not found") from error
    return OperatorGrant(
        grant=catalog.describe_kubernetes_grant(grant), agent_id=agent_id, agent_display_name=display_name
    )


@router.post("/{agent_id}/source/{source_tool_call_id}/revoke", response_model=GrantListResponse)
async def revoke_kubernetes_grant_set(
    agent_id: UUID,
    source_tool_call_id: str,
    body: RevokeGrantRequest,
    actor: OperatorActorDep,
    grants: GrantServiceDep,
    catalog: GrantCatalogDep,
    agents: AgentEnrollmentServiceDep,
) -> GrantListResponse:
    """Revoke one owned Agent's complete grant set from a reviewed source ToolCall."""

    owned = await owned_agent_names(actor=actor, agents=agents)
    display_name = owned.get(agent_id)
    if display_name is None:
        raise HTTPException(status_code=404, detail="Kubernetes grant not found")
    try:
        revoked = await grants.revoke_grant_set(
            owner_agent_id=agent_id, source_tool_call_id=source_tool_call_id, reason=body.reason
        )
    except (GrantNotFoundError, GrantOwnershipError) as error:
        raise HTTPException(status_code=404, detail="Kubernetes grant not found") from error
    return GrantListResponse(
        grants=tuple(
            OperatorGrant(
                grant=catalog.describe_kubernetes_grant(grant), agent_id=agent_id, agent_display_name=display_name
            )
            for grant in revoked
        )
    )
