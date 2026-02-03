"""Agent definitions API routes.

All endpoints require admin access (localhost admin or authenticated admin user).
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from props.backend.auth import require_admin_access
from props.backend.deps import AdminDb
from props.core.agent_types import AgentType
from props.db.models import AgentDefinition

router = APIRouter(dependencies=[Depends(require_admin_access)])


class DefinitionInfo(BaseModel):
    image_digest: str
    agent_type: AgentType
    created_at: datetime


class DefinitionsResponse(BaseModel):
    definitions: list[DefinitionInfo]


@router.get("")
def list_definitions(admin_db: AdminDb, agent_type: AgentType | None = None) -> DefinitionsResponse:
    """List all agent definitions, optionally filtered by type."""
    with admin_db.session() as session:
        query = session.query(AgentDefinition)
        if agent_type:
            query = query.filter_by(agent_type=agent_type)
        definitions = query.order_by(AgentDefinition.created_at.desc()).all()
        return DefinitionsResponse(
            definitions=[
                DefinitionInfo(image_digest=d.digest, agent_type=AgentType(d.agent_type), created_at=d.created_at)
                for d in definitions
            ]
        )
