"""Shared Jinja2 template utilities for agent system prompts.

Used by critic, grader, and other agents to render system prompts
with standard helpers like include_doc() and include_file().
"""

from __future__ import annotations

import importlib.resources
import logging
import os
from pathlib import Path

from jinja2 import Environment

logger = logging.getLogger(__name__)

# Default workspace path for agents
WORKSPACE = Path("/workspace")


def setup_logging() -> None:
    """Configure standard logging for in-container agents."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def create_jinja_env(helpers: dict | None = None, workspace: Path = WORKSPACE) -> Environment:
    """Create Jinja2 environment with standard helpers.

    Args:
        helpers: Optional dict of additional Jinja2 helpers/variables
        workspace: Workspace path (default: /workspace)

    Returns:
        Configured Jinja2 Environment
    """
    env = Environment()
    env.globals["workspace_dir"] = str(workspace)

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


def render_template(content: str, helpers: dict | None = None) -> str:
    """Render a Jinja2 template string with helpers.

    Args:
        content: Template content string
        helpers: Optional dict of Jinja2 helpers/variables

    Returns:
        Rendered template string
    """
    env = create_jinja_env(helpers)
    template = env.from_string(content)
    return template.render()


def load_prompt_template(
    default_resource_path: str = "props/docs/agents/critic.md.j2", env_var: str = "PROMPT_TEMPLATE_PATH"
) -> str:
    """Load prompt template from env var path or default package resource.

    Args:
        default_resource_path: Package path like "props/docs/agents/critic.md.j2"
        env_var: Environment variable name for override path

    Returns:
        Template content string
    """
    prompt_path = os.environ.get(env_var)
    if prompt_path:
        logger.info("Using prompt template from %s", prompt_path)
        return Path(prompt_path).read_text()

    # Load from package resources
    package, _, pkg_path = default_resource_path.partition("/")
    resource = importlib.resources.files(package) / pkg_path
    return resource.read_text()


def render_system_prompt(template_path: str, helpers: dict | None = None, env_var: str | None = None) -> str:
    """Render system prompt from package resource or env var override.

    Args:
        template_path: Package path like "props/docs/agents/grader.md.j2"
        helpers: Optional dict of Jinja2 helpers/variables
        env_var: Optional env var for filesystem override (if set, reads from that path)

    Returns:
        Rendered system prompt
    """
    if env_var:
        content = load_prompt_template(default_resource_path=template_path, env_var=env_var)
    else:
        package, _, pkg_path = template_path.partition("/")
        resource = importlib.resources.files(package) / pkg_path
        content = resource.read_text()

    return render_template(content, helpers)
