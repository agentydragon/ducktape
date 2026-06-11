"""Agent definitions API routes.

Endpoints use agent credential passthrough - RLS policies filter results
based on the caller's database role.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel

from props.backend.auth import CallerDb
from props.core.agent_types import AgentType
from props.db.models import AgentDefinition

router = APIRouter()


class DefinitionInfo(BaseModel):
    image_digest: str
    display_name: str | None
    agent_type: AgentType
    created_at: datetime


class DefinitionsResponse(BaseModel):
    definitions: list[DefinitionInfo]


@router.get("")
def list_definitions(caller_db: CallerDb, agent_type: AgentType | None = None) -> DefinitionsResponse:
    """List all agent definitions, optionally filtered by type."""
    with caller_db.session() as session:
        query = session.query(AgentDefinition)
        if agent_type:
            query = query.filter_by(agent_type=agent_type)
        definitions = query.order_by(AgentDefinition.created_at.desc()).all()
        return DefinitionsResponse(
            definitions=[
                DefinitionInfo(
                    image_digest=d.digest,
                    display_name=d.display_name,
                    agent_type=AgentType(d.agent_type),
                    created_at=d.created_at,
                )
                for d in definitions
            ]
        )
