"""Dependency for Operator-authenticated Agent management routes."""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import Depends, Request

from haku.console.identity.agent_management import AgentManagementService


def _agent_management_service(request: Request) -> AgentManagementService:
    return cast(AgentManagementService, request.app.state.agent_management_service)


AgentManagementServiceDep = Annotated[AgentManagementService, Depends(_agent_management_service)]
