"""Shared utilities for in-container agent loops."""

from __future__ import annotations

import base64
import functools
import importlib.resources
import json
import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import psycopg2
from jinja2 import Environment
from openai import AsyncOpenAI
from psycopg2 import sql

from openai_utils.model import BoundOpenAIModel
from props.core.agent_helpers import get_current_agent_run
from props.db.database import Database

logger = logging.getLogger(__name__)

WORKSPACE = Path("/workspace")


def setup_logging() -> None:
    """Configure logging for in-container agent loops."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def setup_crane_auth() -> None:
    """Configure Docker/crane auth for the OCI registry proxy.

    Derives the registry address from PROPS_BACKEND_URL (the registry proxy
    shares the backend's host:port) and authenticates with PGUSER/PGPASSWORD.
    Writes ~/.docker/config.json so crane can push/pull via Basic auth.
    """
    backend_url = os.environ.get("PROPS_BACKEND_URL", "")
    if not backend_url:
        logger.warning("PROPS_BACKEND_URL not set, skipping crane auth setup")
        return

    username = os.environ["PGUSER"]
    password = os.environ["PGPASSWORD"]

    # Registry proxy is at the same host:port as the backend
    registry = urlparse(backend_url).netloc

    auth_token = base64.b64encode(f"{username}:{password}".encode()).decode()
    config = {"auths": {registry: {"auth": auth_token}}}

    config_dir = Path.home() / ".docker"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.json"
    config_path.write_text(json.dumps(config))
    logger.info("Crane auth configured for registry %s", registry)


def _describe_relation(db: Database, relation_name: str) -> str:
    """Return schema description of a database relation.

    Uses psycopg2's cursor.description to introspect columns. Replacement for
    psql ``\\d+`` that works in distroless containers without psql.
    """
    config = db.config
    conn = psycopg2.connect(
        host=config.host, port=config.port, dbname=config.database, user=config.user, password=config.password
    )
    try:
        with conn.cursor() as cur:
            cur.execute(sql.SQL("SELECT * FROM {} LIMIT 0").format(sql.Identifier(relation_name)))
            if cur.description is None:
                return f"Relation '{relation_name}' not found."

            # Resolve type OIDs to human-readable names via pg_type
            oids = list({col.type_code for col in cur.description})
            cur.execute("SELECT oid, typname FROM pg_catalog.pg_type WHERE oid = ANY(%s)", (oids,))
            type_names = dict(cur.fetchall())

            header = f"{'Column':<30} {'Type':<20}"
            separator = "-" * 50
            lines = [f'Table "{relation_name}"', header, separator]
            for col in cur.description:
                type_name = type_names.get(col.type_code, str(col.type_code))
                lines.append(f"{col.name:<30} {type_name:<20}")

            return "\n".join(lines)
    finally:
        conn.close()


def _setup_jinja_env(db: Database, helpers: dict[str, Any] | None = None) -> Environment:
    """Create Jinja2 environment with standard helpers.

    Globals:
    - workspace_dir — default workspace path
    - describe_relation(name) — schema description via cursor introspection
    - include_doc(pkg/path) — include from package resources
    - include_file(path) — include from filesystem
    """
    env = Environment()
    env.globals["workspace_dir"] = str(WORKSPACE)
    env.globals["describe_relation"] = functools.partial(_describe_relation, db)

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


def render_system_prompt(template_path: str, db: Database, helpers: dict[str, Any] | None = None) -> str:
    """Render system prompt from package resource, returning as string."""
    package, _, pkg_path = template_path.partition("/")
    resource = importlib.resources.files(package) / pkg_path
    return render_template_string(resource.read_text(), db, helpers)


def render_template_string(content: str, db: Database, helpers: dict[str, Any] | None = None) -> str:
    """Render a Jinja2 template string with standard helpers."""
    env = _setup_jinja_env(db, helpers)
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
