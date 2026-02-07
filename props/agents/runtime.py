"""Runtime helpers for agents running inside containers."""

from __future__ import annotations

import importlib.resources
import logging
import os
from pathlib import Path
from typing import Any
from uuid import UUID

from mako.template import Template
from openai import AsyncOpenAI
from sqlalchemy import text
from sqlalchemy.orm import Session

from mako_utils.preprocessor import markdown_heading_preprocessor
from openai_utils.model import BoundOpenAIModel
from props.agents.schema import describe_table
from props.db.database import Database
from props.db.models import AgentRun, AgentRunStatus
from props.db.queries import get_agent_run
from props.db.snapshot_io import fetch_snapshot_to_path

logger = logging.getLogger(__name__)

WORKSPACE = Path("/workspace")


def get_current_agent_run_id(session: Session) -> UUID:
    """Get agent run ID from PostgreSQL current_agent_run_id() function.

    Raises RuntimeError if not connected as an agent user.
    """
    result = session.execute(text("SELECT current_agent_run_id()"))
    agent_run_id = result.scalar()
    if agent_run_id is None:
        raise RuntimeError(
            "current_agent_run_id() returned NULL — not connected as an agent user. "
            "Make sure you're using agent credentials (e.g., agent_{uuid})."
        )
    return UUID(str(agent_run_id))


def get_current_agent_run(session: Session) -> AgentRun:
    """Get the current agent run from database via RLS context.

    Raises ValueError if the run has already exited.
    """
    agent_run_id = get_current_agent_run_id(session)
    agent_run = get_agent_run(session, agent_run_id)
    if agent_run.status == AgentRunStatus.EXITED:
        raise ValueError(f"Agent run {agent_run.agent_run_id} already exited")
    return agent_run


def fetch_snapshot(dest_dir: Path, db: Database) -> Path:
    """Fetch snapshot for current critic agent to specified directory.

    Returns the dest_dir path (for template convenience).
    """
    with db.session() as session:
        agent_run = get_current_agent_run(session)
        critic_config = agent_run.critic_config()
        snapshot_slug = critic_config.example.snapshot_slug

    fetch_snapshot_to_path(snapshot_slug, dest_dir, db)
    return dest_dir


def setup_logging() -> None:
    """Configure logging for in-container agent loops."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _make_template_context(db: Database, helpers: dict[str, Any] | None = None) -> dict[str, Any]:
    """Create Mako template context with standard helpers.

    Available in all templates:
    - workspace_dir — default workspace path
    - describe_relation(name) — schema JSON from SQLAlchemy metadata
    - include_doc(pkg/path) — include and render from package resources
    """
    ctx: dict[str, Any] = {}
    ctx["workspace_dir"] = str(WORKSPACE)

    def _describe_relation(name: str) -> str:
        desc = describe_table(name)
        if desc is None:
            return f"Unknown table: {name}"
        return desc.model_dump_json(indent=2, exclude_defaults=True)

    ctx["describe_relation"] = _describe_relation

    def _source_inspection(binary: str, files: list[tuple[str, str]]) -> str:
        """Generate Source Code Inspection section for agent prompts."""
        lines = [
            "## Source Code Inspection",
            "",
            f"The `props` library is bundled in your container at `/app/{binary}.runfiles/_main/`. Key files:",
            "",
            "```bash",
        ]
        for path, desc in files:
            lines.append(f"cat /app/{binary}.runfiles/_main/{path}   # {desc}")
        lines.append("```")
        lines.append("")
        lines.append("For schema introspection (no DB connection needed), see the Schema Discovery section below.")
        return "\n".join(lines)

    ctx["source_inspection"] = _source_inspection

    def _include(source: str, content: str, *, raw: bool) -> str:
        if raw:
            return f'<doc source="{source}">\n{content}\n</doc>'
        rendered = Template(content, preprocessor=markdown_heading_preprocessor).render(**ctx)
        return f'<doc source="{source}">\n{rendered}\n</doc>'

    def include_doc(pkg_path: str, *, raw: bool = False) -> str:
        """Include doc from package resources, rendering Mako syntax."""
        pkg, _, p = pkg_path.partition("/")
        content = (importlib.resources.files(pkg) / p).read_text()
        return _include(pkg_path, content, raw=raw)

    ctx["include_doc"] = include_doc

    if helpers:
        ctx.update(helpers)

    return ctx


def render_system_prompt(template_path: str, db: Database, helpers: dict[str, Any] | None = None) -> str:
    """Render system prompt from package resource, returning as string."""
    package, _, pkg_path = template_path.partition("/")
    resource = importlib.resources.files(package) / pkg_path
    content = resource.read_text()
    return render_template_string(content, db, helpers)


def render_template_string(content: str, db: Database, helpers: dict[str, Any] | None = None) -> str:
    """Render a Mako template string with standard helpers."""
    ctx = _make_template_context(db, helpers)
    result: str = Template(content, preprocessor=markdown_heading_preprocessor).render(**ctx)
    return result


def create_bound_model_from_env(db: Database) -> BoundOpenAIModel:
    """Create a BoundOpenAIModel using environment variables.

    Gets model from current agent run. Uses OPENAI_BASE_URL and OPENAI_API_KEY.
    """
    with db.session() as session:
        agent_run = get_current_agent_run(session)
        model = agent_run.model

    client = AsyncOpenAI(base_url=os.environ["OPENAI_BASE_URL"], api_key=os.environ["OPENAI_API_KEY"])
    return BoundOpenAIModel(client=client, model=model)
