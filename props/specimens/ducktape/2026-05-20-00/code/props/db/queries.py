"""Database query helpers."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from props.db.models import AgentRun


def get_agent_run(session: Session, agent_run_id: UUID) -> AgentRun:
    """Get agent run by ID. Raises ValueError if not found."""
    agent_run = session.get(AgentRun, agent_run_id)
    if agent_run is None:
        raise ValueError(f"AgentRun not found: {agent_run_id}")
    return agent_run
