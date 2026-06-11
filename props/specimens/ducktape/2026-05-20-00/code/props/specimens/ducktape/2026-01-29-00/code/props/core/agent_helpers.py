"""Helpers for agents running inside the runtime container.

Provides:
- get_current_agent_run_id(): Get agent run ID from PostgreSQL RLS context
- get_scope_description(): Get critic scope description for template rendering
- fetch_snapshot(): Fetch snapshot to local filesystem and return path

For eval API client, use props.core.eval_client.EvalClient.

Database access: Just use get_session() directly - it auto-initializes from PG* env vars.

Usage:

    # Get agent run ID (from database - extracts from username pattern)
    from props.db.session import get_session
    from props.core.agent_helpers import get_current_agent_run_id

    with get_session() as session:
        agent_run_id = get_current_agent_run_id(session)

    # Database access (auto-initializes on first use)
    from props.db.session import get_session
    from props.db.models import Snapshot

    with get_session() as session:
        snapshots = session.query(Snapshot).filter_by(split='train').all()

    # Eval API client for running critics (REST-based)
    from props.core.eval_client import EvalClient, wait_until_graded

    async with EvalClient.from_env() as client:
        result = await client.run_critic(definition_id="critic", example=example)

    # Wait for grading by polling database directly (not via API)
    status = await wait_until_graded(result.critic_run_id)
"""

from __future__ import annotations

import logging
from pathlib import Path
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from props.core.models.examples import WholeSnapshotExample
from props.db.models import AgentRun, FileSet
from props.db.session import get_session
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


def get_scope_description() -> str:
    """Get scope description for critic template.

    Returns a pre-formatted string describing the snapshot and files to review.
    Used as Jinja2 helper in critic.md.j2 template.
    """
    with get_session() as session:
        agent_run = get_current_agent_run(session)
        example = agent_run.critic_config().example

        if isinstance(example, WholeSnapshotExample):
            return f"Snapshot: {example.snapshot_slug}\nReview: ALL files in snapshot"

        # SingleFileSetExample - look up files via FileSet (must exist)
        file_set = (
            session.query(FileSet).filter_by(snapshot_slug=example.snapshot_slug, files_hash=example.files_hash).one()
        )

        files = [member.file_path for member in file_set.members]
        files_str = ", ".join(files)

        return f"Snapshot: {example.snapshot_slug}\nFiles to review: {files_str}"


def fetch_snapshot(dest_dir: Path) -> Path:
    """Fetch snapshot for current critic agent to specified directory.

    Retrieves the tar archive from the snapshots table and extracts it
    to the specified directory.
    Used as Jinja2 helper in critic.md.j2 template.

    Args:
        dest_dir: Destination directory for the snapshot

    Returns:
        The dest_dir path (for template convenience)
    """
    with get_session() as session:
        agent_run = get_current_agent_run(session)
        critic_config = agent_run.critic_config()
        snapshot_slug = critic_config.example.snapshot_slug

    fetch_snapshot_to_path(snapshot_slug, dest_dir)
    return dest_dir
