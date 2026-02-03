"""Helpers for agents running inside the runtime container.

Provides:
- get_current_agent_run_id(): Get agent run ID from PostgreSQL RLS context
- fetch_snapshot(): Fetch snapshot to local filesystem and return path

For eval API client, use props.core.eval_client.EvalClient.
"""

from __future__ import annotations

import logging
from pathlib import Path
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from props.db.database import Database
from props.db.models import AgentRun
from props.db.snapshot_io import fetch_snapshot_to_path

logger = logging.getLogger(__name__)


def get_current_agent_run_id(session: Session) -> UUID:
    """Get agent run ID from PostgreSQL current_agent_run_id() function.

    Raises RuntimeError if not connected as an agent user.
    """
    result = session.execute(text("SELECT current_agent_run_id()"))
    agent_run_id = result.scalar()
    if agent_run_id is None:
        raise RuntimeError(
            "current_agent_run_id() returned NULL - not connected as an agent user. "
            "Make sure you're using agent credentials (e.g., critic_agent_{uuid})."
        )
    if not isinstance(agent_run_id, UUID):
        agent_run_id = UUID(str(agent_run_id))
    return agent_run_id


def get_agent_run(session: Session, agent_run_id: UUID) -> AgentRun:
    """Get agent run by ID. Raises ValueError if not found."""
    agent_run = session.get(AgentRun, agent_run_id)
    if agent_run is None:
        raise ValueError(f"AgentRun not found: {agent_run_id}")
    return agent_run


def get_current_agent_run(session: Session) -> AgentRun:
    """Get the current agent run from database via RLS context."""
    agent_run_id = get_current_agent_run_id(session)
    return get_agent_run(session, agent_run_id)


def fetch_snapshot(dest_dir: Path, db: Database) -> Path:
    """Fetch snapshot for current critic agent to specified directory.

    Retrieves the tar archive from the snapshots table and extracts it
    to the specified directory.

    Returns:
        The dest_dir path (for template convenience)
    """
    with db.session() as session:
        agent_run = get_current_agent_run(session)
        critic_config = agent_run.critic_config()
        snapshot_slug = critic_config.example.snapshot_slug

    fetch_snapshot_to_path(snapshot_slug, dest_dir, db)
    return dest_dir
