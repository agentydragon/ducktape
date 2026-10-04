"""Browser-route access to the app's Agent enrollment service and the acting Operator's roster."""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import Depends, Request

from haku.console.identity.enrollment import AgentEnrollmentService, OperatorAgent
from haku.console.tool_call_actor import OperatorActor


def _enrollment_service(request: Request) -> AgentEnrollmentService:
    return cast(AgentEnrollmentService, request.app.state.agent_enrollment_service)


AgentEnrollmentServiceDep = Annotated[AgentEnrollmentService, Depends(_enrollment_service)]


async def owned_agents(*, actor: OperatorActor, agents: AgentEnrollmentService) -> tuple[OperatorAgent, ...]:
    """The acting Operator's Agents, including the access profile needed for config authority."""

    return await agents.list_agents(operator_id=actor.operator_id)
