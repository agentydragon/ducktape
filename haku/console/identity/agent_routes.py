"""FastAPI boundaries for Operator-authenticated Agent management APIs."""

from __future__ import annotations

import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from haku.console.identity.agent import AgentStatus, CredentialBindingStatus, CredentialKind
from haku.console.identity.enrollment import (
    AccessProfileUnavailableError,
    AgentAccessProfileManagedByDeploymentError,
    AgentNotFoundError,
    OperatorAgent,
)
from haku.console.identity.operator_agents import AgentEnrollmentServiceDep
from haku.console.identity.operator_auth import OperatorSession, operator_session

router = APIRouter(prefix="/api/agent-enrollment", tags=["agents"])


class AgentView(BaseModel):
    agent_id: UUID
    display_name: str
    status: AgentStatus
    credential_kind: CredentialKind
    credential_status: CredentialBindingStatus
    created_at: datetime.datetime
    activated_at: datetime.datetime | None
    last_seen_at: datetime.datetime | None
    access_profile_id: str | None


class AgentListResponse(BaseModel):
    agents: list[AgentView]
    access_profiles: list[str]


class UpdateAgentAccessProfileRequest(BaseModel):
    access_profile_id: str = Field(min_length=1)


OperatorSessionDep = Annotated[OperatorSession | None, Depends(operator_session)]


def _require_operator(session: OperatorSession | None) -> OperatorSession:
    if session is None:
        raise HTTPException(status_code=401, detail="Operator authentication required")
    return session


def _agent_view(agent: OperatorAgent) -> AgentView:
    return AgentView(
        agent_id=agent.agent_id,
        display_name=agent.display_name,
        status=agent.status,
        credential_kind=agent.credential_kind,
        credential_status=agent.credential_status,
        created_at=agent.created_at,
        activated_at=agent.activated_at,
        last_seen_at=agent.last_seen_at,
        access_profile_id=agent.access_profile_id,
    )


@router.get("/agents", response_model=AgentListResponse)
async def list_agents(service: AgentEnrollmentServiceDep, session: OperatorSessionDep) -> AgentListResponse:
    operator = _require_operator(session)
    return AgentListResponse(
        agents=[_agent_view(agent) for agent in await service.list_agents(operator_id=operator.operator_id)],
        access_profiles=list(service.available_access_profiles()),
    )


@router.put("/agents/{agent_id}/access-profile", response_model=AgentView)
async def update_agent_access_profile(
    agent_id: UUID,
    body: UpdateAgentAccessProfileRequest,
    service: AgentEnrollmentServiceDep,
    session: OperatorSessionDep,
) -> AgentView:
    operator = _require_operator(session)
    try:
        agent = await service.set_access_profile(
            operator_id=operator.operator_id, agent_id=agent_id, access_profile_id=body.access_profile_id
        )
    except AgentNotFoundError as error:
        raise HTTPException(status_code=404, detail="Agent not found") from error
    except AgentAccessProfileManagedByDeploymentError as error:
        raise HTTPException(status_code=409, detail="Retired static Agent access profiles are immutable") from error
    except AccessProfileUnavailableError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return _agent_view(agent)
