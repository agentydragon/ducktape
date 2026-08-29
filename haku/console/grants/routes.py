"""Operator browser API for revoking database grants across every grant domain."""

from __future__ import annotations

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from haku.console.grants.catalog import Grant, GrantCatalog
from haku.console.grants.envelope import GRANT_SET_LIMIT, GrantNotFoundError, GrantOwnershipError
from haku.console.identity.operator_agents import AgentEnrollmentServiceDep, owned_agents
from haku.console.identity.operator_auth import OperatorActorDep

router = APIRouter(prefix="/api/grants", tags=["grants"])


class RevokeGrantResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    grants: tuple[Grant, ...]


class RevokeGrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    grant_ids: tuple[UUID, ...] = Field(min_length=1, max_length=GRANT_SET_LIMIT)
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def reason_must_not_be_blank(cls, value: str) -> str:
        reason = value.strip()
        if not reason:
            raise ValueError("revocation reason must not be blank")
        return reason


def _grant_catalog(request: Request) -> GrantCatalog:
    return cast(GrantCatalog, request.app.state.grant_catalog)


GrantCatalogDep = Annotated[GrantCatalog, Depends(_grant_catalog)]


@router.post("/revoke", response_model=RevokeGrantResponse)
async def revoke_grants(
    body: RevokeGrantRequest, actor: OperatorActorDep, catalog: GrantCatalogDep, agents: AgentEnrollmentServiceDep
) -> RevokeGrantResponse:
    """Revoke owned database grants by durable ID, regardless of coverage domain."""

    owner_agent_ids = frozenset(agent.agent_id for agent in await owned_agents(actor=actor, agents=agents))
    if not owner_agent_ids:
        raise HTTPException(status_code=404, detail="Grant not found")
    try:
        revoked = await catalog.revoke_database_grants(
            owner_agent_ids=owner_agent_ids, grant_ids=body.grant_ids, reason=body.reason
        )
    except (GrantNotFoundError, GrantOwnershipError) as error:
        raise HTTPException(status_code=404, detail="Grant not found") from error
    return RevokeGrantResponse(grants=revoked)
