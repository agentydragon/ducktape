"""Output formatting utilities for agent init scripts.

Provides structured output helpers for printing workspace content, running
commands, and processing documentation files with Jinja2 template rendering.
"""

import importlib.resources
import os
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from jinja2 import Environment, Template

# Default workspace path in containers
WORKSPACE = Path("/workspace")


def print_section(title: str) -> None:
    """Print a section header."""
    print(f"=== {title} ===")


def run_command(cmd: str | list[str | os.PathLike[str]], *, shell: bool = False) -> None:
    """Run a command and print output wrapped in <output> tags.

    Args:
        cmd: Command to run (string for shell=True, list for shell=False).
             List elements can be strings or Path objects.
        shell: Whether to run as shell command.

    Raises:
        subprocess.CalledProcessError: If the command fails (check=True).
    """
    if isinstance(cmd, list):
        cmd_strs: str | list[str] = [str(c) for c in cmd]
        cmd_str = " ".join(cmd_strs)
    else:
        cmd_strs = cmd
        cmd_str = cmd
    print(f'<output command="{cmd_str}">')
    subprocess.run(cmd_strs, shell=shell, check=True)
    print("</output>")


def run_command_jinja(cmd: str) -> str:
    """Execute command and return annotated output block.

    For use as Jinja2 template helper: {{ run_command("psql -c '...'") }}

    Args:
        cmd: Shell command to execute.

    Returns:
        Annotated output block with command and stdout.
    """
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, check=True)
    return f'<output command="{cmd}">\n{result.stdout}</output>'


def describe_relation_jinja(relation_name: str) -> str:
    """Return psql \\d+ output for a table or view.

    DRY helper for schema documentation: {{ describe_relation("reported_issues") }}
    """
    return run_command_jinja(f'psql -c "\\d+ {relation_name}"')


def _setup_jinja_env(helpers: Mapping[str, Any] | None = None) -> Environment:
    """Create Jinja2 environment with standard helpers.

    Globals:
    - workspace_dir - default workspace path ("/workspace")

    Helpers:
    - run_command(cmd) - execute shell command
    - describe_relation(name) - psql \\d+ output for tables/views
    - include_doc(pkg/path, raw=False) - include from package resources
    - include_file(path, raw=False) - include from filesystem
    """
    env = Environment()
    env.globals["workspace_dir"] = str(WORKSPACE)
    env.globals["run_command"] = run_command_jinja
    env.globals["describe_relation"] = describe_relation_jinja

    def _include(content: str, source: str) -> str:
        """Include content with Jinja2 rendering and source annotation."""
        rendered = env.from_string(content).render()
        return f'<doc source="{source}">\n{rendered}\n</doc>'

    def include_doc(pkg_path: str, *, raw: bool = False) -> str:
        """Include doc from package resources."""
        pkg, _, p = pkg_path.partition("/")
        content = (importlib.resources.files(pkg) / p).read_text()
        if raw:
            return f'<doc source="{pkg_path}">\n{content}\n</doc>'
        return _include(content, pkg_path)

    def include_file(file_path: str, *, raw: bool = False) -> str:
        """Include file from filesystem."""
        content = Path(file_path).read_text()
        if raw:
            return f'<doc source="{file_path}">\n{content}\n</doc>'
        return _include(content, file_path)

    env.globals["include_doc"] = include_doc
    env.globals["include_file"] = include_file

    if helpers:
        env.globals.update(helpers)

    return env


def render_doc(content: str, helpers: Mapping[str, Any] | None = None) -> str:
    """Render doc content with Jinja2, providing run_command and custom helpers.

    Args:
        content: Markdown content with optional Jinja2 templates.
        helpers: Optional dict of additional Jinja2 helpers to register.

    Returns:
        Rendered content with templates expanded.
    """
    all_helpers: dict[str, Callable[..., Any]] = {"run_command": run_command_jinja}
    if helpers:
        all_helpers.update(helpers)
    template = Template(content)
    return str(template.render(**all_helpers))


def render_and_print_file(path: str | Path, helpers: Mapping[str, Callable[..., Any]] | None = None) -> None:
    """Render a file with Jinja2 and print it.

    Supports include_doc for including package docs from filesystem-based agent docs.

    Args:
        path: Path to the file to render and print.
        helpers: Optional dict of additional Jinja2 helpers to register.
    """
    if isinstance(path, str):
        path = Path(path)
    content = path.read_text()

    env = _setup_jinja_env(helpers)
    template = env.from_string(content)
    rendered = template.render()
    print(f'<file path="{path}">')
    print(rendered)
    print("</file>")


def print_file(path: Path | str, title: str | None = None, workspace: Path = WORKSPACE) -> None:
    """Print a file wrapped in <file> tags.

    Args:
        path: Path to the file (absolute or relative to workspace).
        title: Optional section title to print before the file.
        workspace: Base directory for relative paths (default: /workspace).
    """
    if isinstance(path, str):
        path = Path(path)
    if not path.is_absolute():
        path = workspace / path

    if title:
        print_section(title)
    print(f'<file path="{path}">')
    print(path.read_text())
    print("</file>")


def print_workspace_tree(workspace: Path = WORKSPACE, depth: int = 3) -> None:
    """Print tree of the workspace to show available files.

    Uses the `tree` command with options:
    - -L <depth>: limit depth
    - -a: show hidden files
    - -p: show file permissions (so executable scripts are visible)
    - --noreport: skip the summary line

    Args:
        workspace: Directory to print tree for.
        depth: Maximum depth to traverse.

    Raises:
        subprocess.CalledProcessError: If tree command fails.
    """
    print_section("Workspace Contents")
    run_command(["tree", "-L", str(depth), "-a", "-p", "--noreport", str(workspace)])


def render_agent_prompt(template_path: str, helpers: Mapping[str, Any] | None = None) -> None:
    """Render agent prompt from package resource.

    Supports:
    - {{ include_doc("package/path") }} - include doc with source annotation
    - {{ include_file("/path") }} - include from filesystem
    - {{ describe_relation("name") }} - psql \\d+ output for tables/views
    - {{ run_command("cmd") }} - shell command output

    Args:
        template_path: Package path like "critic_util/docs/agent.md".
        helpers: Optional dict of additional Jinja2 helpers.
    """
    package, _, pkg_path = template_path.partition("/")
    resource = importlib.resources.files(package) / pkg_path
    root_content = resource.read_text()

    env = _setup_jinja_env(helpers)
    template = env.from_string(root_content)
    print(template.render())
