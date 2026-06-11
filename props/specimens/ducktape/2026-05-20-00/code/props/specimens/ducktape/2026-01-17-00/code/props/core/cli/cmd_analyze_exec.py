"""CLI command to analyze docker_exec commands from transcript events."""

from __future__ import annotations

import json
import logging
from collections import Counter
from pathlib import Path
from typing import Annotated

import typer
from rich import box
from rich.console import Console
from rich.table import Table
from sqlalchemy import select

from agent_core.events import ToolCall
from props.core.db.models import Event
from props.core.db.session import get_session

logger = logging.getLogger(__name__)


def cmd_analyze_exec(
    prefix_length: Annotated[int, typer.Option("--prefix-length", help="Length of JSON prefix for grouping")] = 50,
    output_file: Annotated[Path, typer.Option("--output", help="File path to write patterns to")] = Path(
        "docker_exec_patterns.json"
    ),
) -> None:
    """Analyze docker_exec commands from transcripts.

    Extracts docker_exec tool calls from the events table, takes a prefix of the
    JSON representation, and shows frequency histogram of common prefixes.

    Outputs patterns to current directory by default in JSON format.

    Args:
        prefix_length: Length of JSON prefix to use for grouping (default: 50)
        output_file: Path to write patterns to (default: docker_exec_patterns.json)
    """
    console = Console()

    with get_session() as session:
        # Query all tool call events
        stmt = select(Event).where(Event.event_type == "tool_call")
        events = session.execute(stmt).scalars().all()

        if not events:
            console.print("[yellow]No tool call events found in database.[/yellow]")
            return

        # Extract docker_exec commands (just the cmd array)
        exec_commands: list[tuple[str, list[str]]] = []  # (prefix, cmd_array)

        for event in events:
            # Type narrowing - we know it's a ToolCall since event_type == "tool_call"
            if not isinstance(event.payload, ToolCall):
                continue

            # Look for docker_exec or runtime_exec patterns
            if "exec" not in event.payload.name.lower():
                continue

            args_json = event.payload.args_json
            if not args_json:
                continue

            # Parse to extract just the cmd array
            try:
                args = json.loads(args_json)
                if not isinstance(args, dict):
                    logger.debug("Skipping non-dict args for event %s", event.id)
                    continue

                # Try common field names for command
                cmd_array = None
                for field in ["command", "cmd", "commands"]:
                    if field in args:
                        cmd_value = args[field]
                        # Normalize to list format
                        if isinstance(cmd_value, str):
                            cmd_array = [cmd_value]
                        elif isinstance(cmd_value, list):
                            cmd_array = cmd_value
                        break

                if not cmd_array:
                    logger.debug("No cmd field found in args for event %s", event.id)
                    continue

                # Take prefix of the JSON representation of the cmd array
                cmd_json = json.dumps(cmd_array)
                prefix = cmd_json[:prefix_length] if len(cmd_json) > prefix_length else cmd_json
                exec_commands.append((prefix, cmd_array))
            except json.JSONDecodeError as e:
                logger.warning("Failed to parse args_json for event %s: %s", event.id, e)
                continue
            except TypeError as e:
                logger.warning("Type error processing args for event %s: %s", event.id, e)
                continue

        if not exec_commands:
            console.print("[yellow]No docker/runtime exec commands found in events.[/yellow]")
            return

        # Count prefix occurrences
        prefix_counter: Counter[str] = Counter(prefix for prefix, _ in exec_commands)

        # Filter for prefixes that occurred more than once
        repeated_prefixes = {prefix: count for prefix, count in prefix_counter.items() if count > 1}

        if not repeated_prefixes:
            console.print("[yellow]No repeated command prefixes found.[/yellow]")
            return

        # Sort by frequency (descending)
        sorted_prefixes = sorted(repeated_prefixes.items(), key=lambda x: x[1], reverse=True)

        # Display histogram
        console.print(f"\n[bold]Docker/Runtime Exec Command Frequency[/bold] (prefix length: {prefix_length})\n")
        console.print(f"Total tool calls: {len(events)}")
        console.print(f"Exec calls: {len(exec_commands)}")
        console.print(f"Unique prefixes: {len(prefix_counter)}")
        console.print(f"Repeated prefixes: {len(repeated_prefixes)}\n")

        # Create frequency table
        table = Table(show_header=True, header_style="bold cyan", box=box.HORIZONTALS, show_edge=False, padding=(0, 1))
        table.add_column("Count", justify="right", width=6)
        table.add_column("Prefix (JSON)", style="dim")

        for prefix, count in sorted_prefixes:
            # Escape the prefix for display
            display_prefix = prefix.replace("\n", "\\n")
            table.add_row(str(count), display_prefix)

        console.print(table)

        # Write all repeated patterns to JSON
        patterns_data = [{"count": count, "prefix": prefix} for prefix, count in sorted_prefixes]
        with output_file.open("w") as f:
            json.dump(patterns_data, f, indent=2)
        console.print(f"\n[green]Wrote {len(patterns_data)} patterns to {output_file}[/green]")
