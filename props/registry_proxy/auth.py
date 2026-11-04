"""Async authentication for the standalone OCI registry proxy."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request

from props.backend.auth import (
    AdminRole,
    AgentRole,
    AnonymousIdentity,
    AuthenticatedIdentity,
    EvaluatorRole,
    RequestIdentity,
    Role,
    parse_credentials,
    validate_postgres_credentials_async,
)
from props.db.async_database import AsyncDatabase
from props.db.models import AgentRun


def get_registry_db(request: Request) -> AsyncDatabase:
    return request.app.state.admin_db  # type: ignore[no-any-return]


RegistryDb = Annotated[AsyncDatabase, Depends(get_registry_db)]


async def get_registry_identity(request: Request, admin_db: RegistryDb) -> RequestIdentity:
    """Resolve the same PostgreSQL-backed machine identities as the backend.

    The standalone registry has no browser session middleware, so unauthenticated
    requests remain anonymous and receive the OCI Basic challenge in the routes.
    """
    authorization = request.headers.get("authorization")
    if not authorization:
        return AnonymousIdentity()

    parsed = parse_credentials(authorization)
    if not parsed:
        raise HTTPException(status_code=401, detail="Invalid authorization format")

    username, password = parsed
    agent_run_id = await validate_postgres_credentials_async(username, password, admin_db.config)
    if agent_run_id is None:
        role: Role = EvaluatorRole() if username == "evaluator" else AdminRole()
    else:
        async with admin_db.session() as session:
            agent_run = await session.get(AgentRun, agent_run_id)
            if agent_run is None:
                raise HTTPException(status_code=401, detail="Invalid agent token")
            role = AgentRole(agent_type=agent_run.type_config.agent_type, agent_run_id=agent_run_id)

    return AuthenticatedIdentity(username=username, password=password, role=role)


RegistryAuth = Annotated[RequestIdentity, Depends(get_registry_identity)]
