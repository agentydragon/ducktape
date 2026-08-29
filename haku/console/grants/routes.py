"""Operator browser API for revoking database grants across every grant domain."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

from haku.console.grants.catalog import Grant
from haku.console.grants.dependencies import GrantCatalogDep
from haku.console.grants.envelope import GRANT_SET_LIMIT, GrantNotFoundError, GrantOwnershipError
from haku.console.grants.principal import GrantPrincipal
from haku.console.identity.operator_agents import AgentEnrollmentServiceDep, owned_agents
from haku.console.identity.operator_auth import OperatorActorDep

router = APIRouter(prefix="/api/grants", tags=["grants"])


class RevokeGrantResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    grants: tuple[Grant, ...]


class GrantListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    grants: tuple[Grant, ...]


_GRANT_PRINCIPAL: TypeAdapter[GrantPrincipal] = TypeAdapter(GrantPrincipal)


@router.get("", response_model=GrantListResponse)
async def list_grants(
    catalog: GrantCatalogDep,
    principal: Annotated[
        str | None,
        Query(
            description="Optional JSON GrantPrincipal. Omit to list the full catalog; supply one exact declared principal."
        ),
    ] = None,
) -> GrantListResponse:
    """List every declared configuration-file and database grant, optionally for one principal."""

    try:
        declared_principal = _GRANT_PRINCIPAL.validate_json(principal) if principal is not None else None
    except ValidationError as error:
        raise HTTPException(status_code=422, detail="principal must be a JSON GrantPrincipal") from error
    return GrantListResponse(grants=await catalog.list(principal=declared_principal, include_inactive=True))


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
