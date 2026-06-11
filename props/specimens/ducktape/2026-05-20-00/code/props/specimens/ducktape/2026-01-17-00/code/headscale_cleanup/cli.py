"""CLI tool for cleaning up stale Headscale nodes (controlplane* and worker* nodes)."""

import json
import subprocess
from datetime import UTC, datetime
from typing import Annotated, Any, cast

import typer

from cli_util.logging import LogLevel, configure_logging

app = typer.Typer(help="Clean up stale Headscale nodes (controlplane* and worker* nodes).")


def run_headscale_command(args: list[str]) -> None:
    """Run a headscale command."""
    cmd = ["headscale", *args]
    subprocess.run(cmd, capture_output=True, text=True, check=True)


def get_all_nodes() -> list[dict[str, Any]]:
    """Get all nodes from headscale."""
    cmd = ["headscale", "nodes", "list", "-o", "json"]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return cast(list[dict[str, Any]], json.loads(result.stdout))


def filter_stale_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter nodes to only include controlplane* and worker* nodes."""
    stale_nodes = []
    for node in nodes:
        name = node.get("name", "")
        if name.startswith(("controlplane", "worker")):
            stale_nodes.append(node)
    return stale_nodes


def format_last_seen(last_seen_data: dict[str, Any]) -> str:
    """Format last seen timestamp for display."""
    if not last_seen_data:
        return "Never"

    seconds = last_seen_data.get("seconds", 0)
    if seconds == 0:
        return "Never"

    last_seen = datetime.fromtimestamp(seconds, tz=UTC)
    now = datetime.now(UTC)

    diff = now - last_seen
    days = diff.days
    hours, remainder = divmod(diff.seconds, 3600)
    minutes, _ = divmod(remainder, 60)

    if days > 0:
        return f"{days}d {hours}h ago"
    if hours > 0:
        return f"{hours}h {minutes}m ago"
    return f"{minutes}m ago"


def is_node_offline(node: dict[str, Any]) -> bool:
    """Check if a node appears to be offline based on last_seen."""
    last_seen = node.get("last_seen", {})
    if not last_seen:
        return True

    seconds = int(last_seen.get("seconds", 0))
    if seconds == 0:
        return True

    # Consider offline if not seen in last hour
    now = datetime.now(UTC).timestamp()
    return (now - seconds) > 3600


def display_nodes(nodes: list[dict[str, Any]]) -> None:
    """Display nodes in a formatted table."""
    if not nodes:
        typer.echo("No controlplane or worker nodes found.")
        return

    typer.echo(f"{'ID':<4} {'Name':<25} {'IP Address':<15} {'Status':<8} {'Last Seen'}")
    typer.echo("-" * 80)

    for node in nodes:
        node_id = node.get("id", "?")
        name = node.get("name", "unknown")
        ip_addresses = node.get("ip_addresses", [])
        ip_addr = ip_addresses[0] if ip_addresses else "none"

        status = "OFFLINE" if is_node_offline(node) else "online"
        last_seen = format_last_seen(node.get("last_seen", {}))

        typer.echo(f"{node_id:<4} {name:<25} {ip_addr:<15} {status:<8} {last_seen}")


def select_nodes_for_deletion(nodes: list[dict[str, Any]], all_offline: bool = False) -> list[int]:
    """Let user select which nodes to delete."""
    offline_nodes = [node for node in nodes if is_node_offline(node)]

    if not offline_nodes:
        typer.echo("\nNo offline nodes found to delete.")
        return []

    if all_offline:
        typer.echo(f"\nAuto-selecting all {len(offline_nodes)} offline nodes for deletion.")
        return [node["id"] for node in offline_nodes]

    typer.echo(f"\nFound {len(offline_nodes)} offline nodes:")
    display_nodes(offline_nodes)

    while True:
        response = typer.prompt(f"\nDelete all {len(offline_nodes)} offline nodes? (y/n/list)").lower().strip()
        if response in {"y", "yes"}:
            return [node["id"] for node in offline_nodes]
        if response in {"n", "no"}:
            return []
        if response == "list":
            display_nodes(offline_nodes)
        else:
            typer.echo("Please enter 'y', 'n', or 'list'")


def delete_nodes(node_ids: list[int], dry_run: bool = False, force: bool = False) -> None:
    """Delete the specified nodes."""
    if not node_ids:
        typer.echo("No nodes to delete.")
        return

    if dry_run:
        typer.echo(f"\n[DRY RUN] Would delete {len(node_ids)} nodes with IDs: {', '.join(map(str, node_ids))}")
        return

    if not force:
        typer.echo(f"\nAbout to delete {len(node_ids)} nodes with IDs: {', '.join(map(str, node_ids))}")
        confirm = typer.prompt("Are you absolutely sure? Type 'DELETE' to confirm")
        if confirm != "DELETE":
            typer.echo("Deletion cancelled.")
            return

    typer.echo(f"\nDeleting {len(node_ids)} nodes...")

    deleted_count = 0

    for i, node_id in enumerate(node_ids, 1):
        progress_percent = (i / len(node_ids)) * 100
        typer.echo(f"[{i:>3}/{len(node_ids):>3}] ({progress_percent:5.1f}%) Deleting node {node_id}... ", nl=False)

        run_headscale_command(["nodes", "delete", "-i", str(node_id), "--force"])
        typer.echo("✓")
        deleted_count += 1

    typer.echo(f"\nDeletion complete: {deleted_count} deleted")


@app.callback(invoke_without_command=True)
def cleanup(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would be deleted without deleting")] = False,
    force: Annotated[bool, typer.Option("--force", help="Skip confirmation prompts")] = False,
    all_offline: Annotated[bool, typer.Option("--all-offline", help="Automatically select all offline nodes")] = False,
    log_output: Annotated[
        str,
        typer.Option("--log-output", envvar="ADGN_LOG_OUTPUT", help="Log output: stderr, stdout, none, or file path"),
    ] = "stderr",
    log_level: Annotated[
        str,
        typer.Option("--log-level", envvar="ADGN_LOG_LEVEL", help="Log level: DEBUG, INFO, WARNING, ERROR, CRITICAL"),
    ] = LogLevel.WARNING,
) -> None:
    """Clean up stale controlplane and worker nodes from Headscale."""
    configure_logging(log_output=log_output, log_level=log_level)
    typer.echo("Fetching nodes from Headscale...")
    all_nodes = get_all_nodes()
    stale_nodes = filter_stale_nodes(all_nodes)

    if not stale_nodes:
        typer.echo("No controlplane or worker nodes found.")
        return

    typer.echo(f"\nFound {len(stale_nodes)} controlplane/worker nodes:")
    display_nodes(stale_nodes)

    nodes_to_delete = select_nodes_for_deletion(stale_nodes, all_offline)

    if nodes_to_delete:
        delete_nodes(nodes_to_delete, dry_run, force)
        typer.echo(f"\nOperation completed. Deleted {len(nodes_to_delete)} nodes.")
    else:
        typer.echo("\nNo nodes selected for deletion.")


def main() -> None:
    """Entry point for the CLI."""
    app()


if __name__ == "__main__":
    main()
