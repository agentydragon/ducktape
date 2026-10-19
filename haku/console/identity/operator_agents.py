"""Dependency for Operator-authenticated Agent management routes."""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import Depends, Request

from haku.console.identity.enrollment import AgentEnrollmentService


def _enrollment_service(request: Request) -> AgentEnrollmentService:
    return cast(AgentEnrollmentService, request.app.state.agent_enrollment_service)


AgentEnrollmentServiceDep = Annotated[AgentEnrollmentService, Depends(_enrollment_service)]
