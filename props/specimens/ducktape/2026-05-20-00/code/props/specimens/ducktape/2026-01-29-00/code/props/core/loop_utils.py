"""Shared utilities for in-container agent loops."""

from __future__ import annotations

import importlib.resources
import logging
import os
from pathlib import Path

from jinja2 import Environment
from openai import AsyncOpenAI

from openai_utils.model import BoundOpenAIModel
from props.core.agent_helpers import get_current_agent_run
from props.db.session import get_session

WORKSPACE = Path("/workspace")


def setup_logging() -> None:
    """Configure logging for in-container agent loops."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _setup_jinja_env(helpers: dict | None = None) -> Environment:
    """Create Jinja2 environment with standard helpers."""
    env = Environment()
    env.globals["workspace_dir"] = str(WORKSPACE)

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


def render_system_prompt(template_path: str, helpers: dict | None = None) -> str:
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


def create_bound_model_from_env() -> BoundOpenAIModel:
    """Create a BoundOpenAIModel using environment variables.

    Gets model from current agent run. Uses OPENAI_BASE_URL and OPENAI_API_KEY.
    """
    with get_session() as session:
        agent_run = get_current_agent_run(session)
        model = agent_run.model

    client = AsyncOpenAI(
        base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        api_key=os.environ.get("OPENAI_API_KEY", ""),
    )
    return BoundOpenAIModel(client=client, model=model)
