"""Shared utilities for in-container agent loops."""

from __future__ import annotations

import importlib.resources
import logging
import os
from pathlib import Path
from typing import Any

import psycopg2
from jinja2 import Environment
from openai import AsyncOpenAI

from openai_utils.model import BoundOpenAIModel
from props.core.agent_helpers import get_current_agent_run
from props.db.database import Database

logger = logging.getLogger(__name__)

WORKSPACE = Path("/workspace")


def setup_logging() -> None:
    """Configure logging for in-container agent loops."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _describe_relation(relation_name: str) -> str:
    """Return schema description of a database relation.

    Uses PG* environment variables for connection. Replacement for
    psql ``\\d+`` that works in distroless containers without psql.
    """
    conn = psycopg2.connect(
        host=os.environ.get("PGHOST", "localhost"),
        port=int(os.environ.get("PGPORT", "5432")),
        dbname=os.environ.get("PGDATABASE", "props"),
        user=os.environ.get("PGUSER", ""),
        password=os.environ.get("PGPASSWORD", ""),
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name, data_type, is_nullable, column_default
                FROM information_schema.columns
                WHERE table_name = %s
                ORDER BY ordinal_position
                """,
                (relation_name,),
            )
            rows = cur.fetchall()

            if not rows:
                return f"Relation '{relation_name}' not found."

            header = f"{'Column':<30} {'Type':<30} {'Nullable':<8} {'Default'}"
            separator = "-" * len(header)
            lines = [f'Table "{relation_name}"', header, separator]
            for col_name, data_type, nullable, default in rows:
                default_str = str(default) if default else ""
                lines.append(f"{col_name:<30} {data_type:<30} {nullable:<8} {default_str}")

            return "\n".join(lines)
    finally:
        conn.close()


def _setup_jinja_env(helpers: dict[str, Any] | None = None) -> Environment:
    """Create Jinja2 environment with standard helpers.

    Globals:
    - workspace_dir — default workspace path
    - describe_relation(name) — schema description via information_schema
    - include_doc(pkg/path) — include from package resources
    - include_file(path) — include from filesystem
    """
    env = Environment()
    env.globals["workspace_dir"] = str(WORKSPACE)
    env.globals["describe_relation"] = _describe_relation

    def include_doc(pkg_path: str, *, raw: bool = False) -> str:
        """Include doc from package resources."""
        pkg, _, p = pkg_path.partition("/")
        content = (importlib.resources.files(pkg) / p).read_text()
        if raw:
            return f'<doc source="{pkg_path}">\n{content}\n</doc>'
        rendered = env.from_string(content).render()
        return f'<doc source="{pkg_path}">\n{rendered}\n</doc>'

    def include_file(file_path: str, *, raw: bool = False) -> str:
        """Include file from filesystem."""
        content = Path(file_path).read_text()
        if raw:
            return f'<doc source="{file_path}">\n{content}\n</doc>'
        rendered = env.from_string(content).render()
        return f'<doc source="{file_path}">\n{rendered}\n</doc>'

    env.globals["include_doc"] = include_doc
    env.globals["include_file"] = include_file

    if helpers:
        env.globals.update(helpers)

    return env


def render_system_prompt(template_path: str, helpers: dict[str, Any] | None = None) -> str:
    """Render system prompt from package resource, returning as string.

    Args:
        template_path: Package path like "props/docs/agents/grader.md.j2"
        helpers: Optional dict of additional Jinja2 helpers

    Returns:
        Rendered system prompt
    """
    package, _, pkg_path = template_path.partition("/")
    resource = importlib.resources.files(package) / pkg_path
    root_content = resource.read_text()

    env = _setup_jinja_env(helpers)
    template = env.from_string(root_content)
    return template.render()


def render_template_string(content: str, helpers: dict[str, Any] | None = None) -> str:
    """Render a Jinja2 template string with standard helpers."""
    env = _setup_jinja_env(helpers)
    return env.from_string(content).render()


def create_bound_model_from_env(db: Database) -> BoundOpenAIModel:
    """Create a BoundOpenAIModel using environment variables.

    Gets model from current agent run. Uses OPENAI_BASE_URL and OPENAI_API_KEY.
    """
    with db.session() as session:
        agent_run = get_current_agent_run(session)
        model = agent_run.model

    client = AsyncOpenAI(
        base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        api_key=os.environ.get("OPENAI_API_KEY", ""),
    )
    return BoundOpenAIModel(client=client, model=model)
