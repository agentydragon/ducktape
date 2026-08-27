"""Operator browser API for inspecting temporary HTTP egress grants.

Inspection only: revocation is the ``http_grants`` MCP server's ``revoke_grants`` tool, which the
trusted frontend calls through Operator-direct MCP execution rather than a bespoke route.
"""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from haku.console.http_grant_models import HttpGrant
from haku.console.http_grant_service import HttpGrantService
from haku.console.operator_agents import AgentEnrollmentServiceDep, owned_agent_names
from haku.console.operator_auth import OperatorActorDep

router = APIRouter(prefix="/api/http-grants", tags=["http-grants"])


class OperatorHttpGrant(BaseModel):
    """One grant plus the operator-owned Agent name used by the browser."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    grant: HttpGrant
    agent_display_name: str


class HttpGrantListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    grants: tuple[OperatorHttpGrant, ...]


def _grant_service(request: Request) -> HttpGrantService:
    return cast(HttpGrantService, request.app.state.http_grants)


GrantServiceDep = Annotated[HttpGrantService, Depends(_grant_service)]


@router.get("", response_model=HttpGrantListResponse)
async def list_http_grants(
    actor: OperatorActorDep, grants: GrantServiceDep, agents: AgentEnrollmentServiceDep
) -> HttpGrantListResponse:
    """List active and historical grants for only this Operator's Agents."""

    owned = await owned_agent_names(actor=actor, agents=agents)
    records = [
        OperatorHttpGrant(grant=grant, agent_display_name=display_name)
        for agent_id, display_name in owned.items()
        for grant in await grants.list_grants(owner_agent_id=agent_id, include_terminal=True)
    ]
    records.sort(key=lambda item: (item.grant.created_at, item.grant.grant_id), reverse=True)
    return HttpGrantListResponse(grants=tuple(records))
