"""Agent package CLI commands.

Agent definitions are now stored as OCI images in the registry.
Use Bazel to build and push images:
    bazel run //props/core/critic:push
    bazel run //props/core/grader:push
"""

from __future__ import annotations

from typing import Annotated

import typer

from props.core.agent_types import AgentType
from props.db.models import AgentDefinition
from props.db.session import get_session

app = typer.Typer(name="agent-pkg", help="Agent package management commands", add_completion=False)


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


# validate command removed - use 'bazel build //props/core/{agent}:image' instead
