"""Helpers for agents running inside the runtime container.

Provides:
- get_current_agent_run_id(): Get agent run ID from PostgreSQL RLS context
- get_scope_description(): Get critic scope description for template rendering
- fetch_snapshot(): Fetch snapshot to local filesystem and return path

For MCP client, use agent_pkg_runtime.mcp_client_from_env directly.

Database access: Just use get_session() directly - it auto-initializes from PG* env vars.

Usage:

    # Get agent run ID (from database - extracts from username pattern)
    from props.core.db.session import get_session
    from props.core.agent_helpers import get_current_agent_run_id

    with get_session() as session:
        agent_run_id = get_current_agent_run_id(session)

    # Database access (auto-initializes on first use)
    from props.core.db.session import get_session
    from props.core.db.models import Snapshot

    with get_session() as session:
        snapshots = session.query(Snapshot).filter_by(split='train').all()

    # MCP HTTP client (from agent_pkg.runtime.mcp)
    from agent_pkg.runtime.mcp import mcp_client_from_env

    async with mcp_client_from_env() as (client, _):
        result = await client.call_tool("tool_name", {"arg": "value"})
"""

from __future__ import annotations

import logging
from pathlib import Path
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from props.core.cli.cmd_snapshot import fetch_snapshot_to_path
from props.core.db.models import AgentRun, FileSet
from props.core.db.session import get_session
from props.core.models.examples import WholeSnapshotExample

logger = logging.getLogger(__name__)


def get_current_agent_run_id(session: Session) -> UUID:
    """Get the current agent run ID from the database.

    Uses the PostgreSQL current_agent_run_id() function which extracts
    the UUID from the database username (e.g., agent_{uuid} pattern).

    This is the canonical way to get the current agent's run ID when running
    inside the container. The database extracts the ID from the agent user's
    username pattern.

    Args:
        session: Active SQLAlchemy session

    Returns:
        UUID of the current agent run

    Raises:
        RuntimeError: If not connected as an agent user, or if the
                      current_agent_run_id() function returns NULL
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


def get_current_agent_run(session: Session) -> AgentRun:
    """Get the current agent run ORM object from the database.

    Combines get_current_agent_run_id() with loading the AgentRun record.
    Use this when you need the full AgentRun object with typed access to
    type_config via methods like prompt_optimizer_config().

    Args:
        session: Active SQLAlchemy session

    Returns:
        AgentRun object for the current agent

    Raises:
        RuntimeError: If not connected as an agent user
        ValueError: If agent run record not found in database

    Example:
        with get_session() as session:
            run = get_current_agent_run(session)
            config = run.prompt_optimizer_config()  # Type-safe access
            print(f"Target metric: {config.target_metric}")
    """
    agent_run_id = get_current_agent_run_id(session)
    agent_run = session.get(AgentRun, agent_run_id)
    if agent_run is None:
        raise ValueError(f"AgentRun not found for agent_run_id={agent_run_id}")
    return agent_run


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
