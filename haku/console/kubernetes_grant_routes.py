"""Operator browser API for inspecting and revoking temporary Kubernetes grants."""

from __future__ import annotations

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from haku.console.agents.enrollment import AgentEnrollmentService
from haku.console.kubernetes_grant_models import (
    KubernetesGrant,
    KubernetesGrantNotFoundError,
    KubernetesGrantOwnershipError,
)
from haku.console.kubernetes_grant_service import KubernetesGrantService
from haku.console.operator_auth import OperatorActorDep

router = APIRouter(prefix="/api/kubernetes-grants", tags=["kubernetes-grants"])


class OperatorKubernetesGrant(BaseModel):
    """One grant plus the operator-owned Agent name used by the browser."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    grant: KubernetesGrant
    agent_display_name: str


class KubernetesGrantListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    grants: tuple[OperatorKubernetesGrant, ...]


class RevokeKubernetesGrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def reason_must_not_be_blank(cls, value: str) -> str:
        reason = value.strip()
        if not reason:
            raise ValueError("revocation reason must not be blank")
        return reason


def _grant_service(request: Request) -> KubernetesGrantService:
    return cast(KubernetesGrantService, request.app.state.kubernetes_grants)


def _agent_service(request: Request) -> AgentEnrollmentService:
    return cast(AgentEnrollmentService, request.app.state.agent_enrollment_service)


GrantServiceDep = Annotated[KubernetesGrantService, Depends(_grant_service)]
AgentServiceDep = Annotated[AgentEnrollmentService, Depends(_agent_service)]


async def _owned_agents(*, actor: OperatorActorDep, agents: AgentServiceDep) -> dict[UUID, str]:
    return {agent.agent_id: agent.display_name for agent in await agents.list_agents(operator_id=actor.operator_id)}


@router.get("", response_model=KubernetesGrantListResponse)
async def list_kubernetes_grants(
    actor: OperatorActorDep, grants: GrantServiceDep, agents: AgentServiceDep
) -> KubernetesGrantListResponse:
    """List active and historical grants for only this Operator's Agents."""

    owned = await _owned_agents(actor=actor, agents=agents)
    records = [
        OperatorKubernetesGrant(grant=grant, agent_display_name=display_name)
        for agent_id, display_name in owned.items()
        for grant in await grants.list_grants(owner_agent_id=agent_id, include_terminal=True)
    ]
    records.sort(key=lambda item: (item.grant.created_at, item.grant.grant_id), reverse=True)
    return KubernetesGrantListResponse(grants=tuple(records))


@router.post("/{agent_id}/{grant_id}/revoke", response_model=OperatorKubernetesGrant)
async def revoke_kubernetes_grant(
    agent_id: UUID,
    grant_id: UUID,
    body: RevokeKubernetesGrantRequest,
    actor: OperatorActorDep,
    grants: GrantServiceDep,
    agents: AgentServiceDep,
) -> OperatorKubernetesGrant:
    """Revoke one owned Agent's grant with a durable, operator-supplied reason."""

    owned = await _owned_agents(actor=actor, agents=agents)
    display_name = owned.get(agent_id)
    if display_name is None:
        raise HTTPException(status_code=404, detail="Kubernetes grant not found")
    try:
        grant = await grants.revoke_grant(owner_agent_id=agent_id, grant_id=grant_id, reason=body.reason)
    except (KubernetesGrantNotFoundError, KubernetesGrantOwnershipError) as error:
        raise HTTPException(status_code=404, detail="Kubernetes grant not found") from error
    return OperatorKubernetesGrant(grant=grant, agent_display_name=display_name)


@router.post("/{agent_id}/source/{source_tool_call_id}/revoke", response_model=KubernetesGrantListResponse)
async def revoke_kubernetes_grant_set(
    agent_id: UUID,
    source_tool_call_id: str,
    body: RevokeKubernetesGrantRequest,
    actor: OperatorActorDep,
    grants: GrantServiceDep,
    agents: AgentServiceDep,
) -> KubernetesGrantListResponse:
    """Revoke one owned Agent's complete grant set from a reviewed source ToolCall."""

    owned = await _owned_agents(actor=actor, agents=agents)
    display_name = owned.get(agent_id)
    if display_name is None:
        raise HTTPException(status_code=404, detail="Kubernetes grant not found")
    try:
        revoked = await grants.revoke_grant_set(
            owner_agent_id=agent_id, source_tool_call_id=source_tool_call_id, reason=body.reason
        )
    except (KubernetesGrantNotFoundError, KubernetesGrantOwnershipError) as error:
        raise HTTPException(status_code=404, detail="Kubernetes grant not found") from error
    return KubernetesGrantListResponse(
        grants=tuple(OperatorKubernetesGrant(grant=grant, agent_display_name=display_name) for grant in revoked)
    )
