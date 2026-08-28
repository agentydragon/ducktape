"""Operator browser API for inspecting temporary HTTP egress grants.

Inspection only: revocation is the shared ``grants`` MCP server's ``revoke_grants`` tool (domain
``http``), which the trusted frontend calls through Operator-direct MCP execution rather than a
bespoke route.
"""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from haku.console.grants.http.models import Grant
from haku.console.grants.http.service import GrantService
from haku.console.operator_agents import AgentEnrollmentServiceDep, owned_agent_names
from haku.console.operator_auth import OperatorActorDep

router = APIRouter(prefix="/api/http-grants", tags=["http-grants"])


class OperatorGrant(BaseModel):
    """One grant plus the operator-owned Agent name used by the browser."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    grant: Grant
    agent_display_name: str


class GrantListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    grants: tuple[OperatorGrant, ...]


def _grant_service(request: Request) -> GrantService:
    return cast(GrantService, request.app.state.http_grants)


GrantServiceDep = Annotated[GrantService, Depends(_grant_service)]


@router.get("", response_model=GrantListResponse)
async def list_http_grants(
    actor: OperatorActorDep, grants: GrantServiceDep, agents: AgentEnrollmentServiceDep
) -> GrantListResponse:
    """List active and historical grants for only this Operator's Agents."""

    owned = await owned_agent_names(actor=actor, agents=agents)
    records = [
        OperatorGrant(grant=grant, agent_display_name=display_name)
        for agent_id, display_name in owned.items()
        for grant in await grants.list_grants(owner_agent_id=agent_id, include_terminal=True)
    ]
    records.sort(key=lambda item: (item.grant.created_at, item.grant.grant_id), reverse=True)
    return GrantListResponse(grants=tuple(records))
