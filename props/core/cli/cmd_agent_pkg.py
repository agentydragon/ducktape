"""Agent package CLI commands.

Provides CLI access to agent package management:
- create: Pack directory into tar archive and insert into database
- fetch: Extract package from database and unpack to directory

Structure:
    props agent-pkg create <dir> [--id <id>] [--type <type>]
    props agent-pkg fetch <id> <target-dir>
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from props.core.agent_types import AgentType
from props.core.db.models import AgentDefinition
from props.core.db.session import get_session

app = typer.Typer(name="agent-pkg", help="Agent package management commands", add_completion=False)


@app.command("create")
def cmd_create(
    pkg_dir: Annotated[Path, typer.Argument(help="Directory containing agent package")],
    pkg_id: Annotated[str | None, typer.Option("--id", help="Package ID (auto-generated if not provided)")] = None,
    agent_type: Annotated[AgentType, typer.Option("--type", help="Agent type")] = AgentType.FREEFORM,
    force: Annotated[bool, typer.Option("--force", "-f", help="Overwrite existing package")] = False,
) -> None:
    """DEPRECATED: Pack directory into tar archive - no longer functional.

    Agent definitions are now stored as OCI images in the registry.
    Use 'docker build' + 'docker push' to create agent definitions instead.

    The archive column has been removed from agent_definitions table.
    """
    typer.echo(
        "Error: 'props agent-pkg create' is deprecated.\n"
        "Agent definitions are now OCI images managed via registry proxy.\n"
        "Use 'docker build' + 'docker push' to registry instead.",
        err=True,
    )
    raise typer.Exit(1)


@app.command("fetch")
def cmd_fetch(
    pkg_id: Annotated[str, typer.Argument(help="Package ID to fetch")],
    target_dir: Annotated[Path, typer.Argument(help="Target directory to unpack to")],
    force: Annotated[bool, typer.Option("--force", "-f", help="Overwrite existing directory")] = False,
) -> None:
    """DEPRECATED: Extract package from database - no longer functional.

    Agent definitions are now stored as OCI images in the registry.
    Use 'docker pull' to fetch images instead.

    The archive column has been removed from agent_definitions table.
    """
    typer.echo(
        "Error: 'props agent-pkg fetch' is deprecated.\n"
        "Agent definitions are now OCI images managed via registry proxy.\n"
        "Use 'docker pull' from registry instead.",
        err=True,
    )
    raise typer.Exit(1)


@app.command("list")
def cmd_list(
    agent_type: Annotated[AgentType | None, typer.Option("--type", help="Filter by agent type")] = None,
) -> None:
    """List all agent definitions (OCI images) in database."""
    with get_session() as session:
        query = session.query(AgentDefinition)
        if agent_type:
            query = query.filter_by(agent_type=agent_type)
        definitions = query.order_by(AgentDefinition.created_at.desc()).all()

        if not definitions:
            typer.echo("No agent definitions found")
            return

        typer.echo(f"Found {len(definitions)} agent definitions:\n")
        for defn in definitions:
            created_by = f" (by {defn.created_by_agent_run_id})" if defn.created_by_agent_run_id else ""
            typer.echo(f"  {defn.digest} [{defn.agent_type}]{created_by}")


@app.command("validate")
def cmd_validate(pkg_dir: Annotated[Path, typer.Argument(help="Directory containing agent package")]) -> None:
    """DEPRECATED: Validate agent package - no longer functional.

    Agent definitions are now OCI images in the registry.
    Use 'docker build' to validate image builds instead.
    """
    typer.echo(
        "Error: 'props agent-pkg validate' is deprecated.\n"
        "Agent definitions are now OCI images.\n"
        "Use 'docker build' to validate instead.",
        err=True,
    )
    raise typer.Exit(1)
