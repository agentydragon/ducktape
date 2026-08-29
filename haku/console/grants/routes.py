"""Operator browser API for revoking database grants across every grant domain."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from haku.console.grants.catalog import DatabaseGrantSource, Grant
from haku.console.grants.dependencies import GrantCatalogDep
from haku.console.grants.envelope import GRANT_SET_LIMIT, GrantNotFoundError, GrantOwnershipError
from haku.console.identity.operator_agents import AgentEnrollmentServiceDep, owned_agents
from haku.console.identity.operator_auth import OperatorActorDep

router = APIRouter(prefix="/api/grants", tags=["grants"])


class RevokeGrantResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    grants: tuple[Grant, ...]


class AgentGrant(BaseModel):
    """One effective grant and the Agent it belongs to."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    grant: Grant
    agent_id: UUID


class GrantListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    grants: tuple[AgentGrant, ...]


@router.get("", response_model=GrantListResponse)
async def list_grants(
    actor: OperatorActorDep, catalog: GrantCatalogDep, agents: AgentEnrollmentServiceDep
) -> GrantListResponse:
    """List configuration and database authority across every grant domain."""

    owned = await owned_agents(actor=actor, agents=agents)
    records = [
        AgentGrant(grant=grant, agent_id=agent.agent_id)
        for agent in owned
        for grant in await catalog.list_for_agent(agent_id=agent.agent_id, access_profile_id=agent.access_profile_id)
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


class RevokeGrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    grant_ids: tuple[UUID, ...] = Field(min_length=1, max_length=GRANT_SET_LIMIT)
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None


@router.post("/revoke", response_model=RevokeGrantResponse)
async def revoke_grants(
    body: RevokeGrantRequest, actor: OperatorActorDep, catalog: GrantCatalogDep, agents: AgentEnrollmentServiceDep
) -> RevokeGrantResponse:
    """Revoke owned database grants by durable ID, regardless of coverage domain."""

    owner_agent_ids = frozenset(agent.agent_id for agent in await owned_agents(actor=actor, agents=agents))
    if not owner_agent_ids:
        raise HTTPException(status_code=404, detail="Grant not found")
    try:
        revoked = await catalog.end_database_grants(
            owner_agent_ids=owner_agent_ids, grant_ids=body.grant_ids, reason=body.reason
        )
    except (GrantNotFoundError, GrantOwnershipError) as error:
        raise HTTPException(status_code=404, detail="Grant not found") from error
    return RevokeGrantResponse(grants=revoked)
