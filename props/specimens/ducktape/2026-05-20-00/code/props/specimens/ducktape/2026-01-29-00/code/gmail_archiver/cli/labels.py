"""Gmail labels subcommands."""

from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from gmail_archiver.cli.common import DryRunOption, TokenFileOption, get_client

labels_app = typer.Typer(help="Manage Gmail labels")


@labels_app.command("list")
def list_labels(
    show_system: Annotated[bool, typer.Option("--system", "-s", help="Include system labels")] = False,
    token_file: TokenFileOption = None,
):
    """List all labels with filter usage info."""
    console = Console()
    client = get_client(token_file)

    console.print("Fetching labels and filters...")

    labels = client.list_labels_full()
    filters = client.list_filters()

    used_label_ids: set[str] = set()
    for f in filters:
        used_label_ids.update(f.action.add_label_ids)
        used_label_ids.update(f.action.remove_label_ids)

    table = Table(title="Gmail Labels")
    table.add_column("Name", style="cyan")
    table.add_column("Type", style="dim")
    table.add_column("Used by Filter", style="green")
    table.add_column("ID", style="dim", max_width=30)

    user_labels = []
    system_labels = []

    for label in labels:
        label_type = label.type or "user"
        is_system = label_type == "system"

        if is_system and not show_system:
            continue

        is_used = label.id in used_label_ids
        row = (label.name, label_type, "✓" if is_used else "", label.id)

        if is_system:
            system_labels.append(row)
        else:
            user_labels.append(row)

    for row in sorted(user_labels, key=lambda x: x[0].lower()):
        table.add_row(*row)

    if show_system:
        if user_labels and system_labels:
            table.add_row("", "", "", "")  # Separator
        for row in sorted(system_labels, key=lambda x: x[0]):
            table.add_row(*row)

    console.print(table)

    user_count = len(user_labels)
    used_count = sum(1 for _, _, used, _ in user_labels if used == "✓")
    unused_count = user_count - used_count

    console.print(f"\nUser labels: {user_count} total, {used_count} used by filters, {unused_count} unused")

    if unused_count > 0:
        console.print("\n[dim]Tip: Run 'gmail-archiver labels prune' to see unused labels[/dim]")


@labels_app.command("prune")
def prune(dry_run: DryRunOption = None, token_file: TokenFileOption = None):
    """Delete labels not referenced by any Gmail filter.

    Only user labels (not system labels) are considered for pruning.
    """
    console = Console()
    client = get_client(token_file)

    console.print("Fetching labels and filters...")

    labels = client.list_labels_full()
    filters = client.list_filters()

    used_label_ids: set[str] = set()
    for f in filters:
        used_label_ids.update(f.action.add_label_ids)

    unused_labels = [label for label in labels if label.type != "system" and label.id not in used_label_ids]

    if not unused_labels:
        console.print("[green]✓[/green] All user labels are used by filters. Nothing to prune.")
        return

    console.print(f"\n[bold]Unused labels ({len(unused_labels)}):[/bold]")
    table = Table()
    table.add_column("Name", style="yellow")
    table.add_column("ID", style="dim", max_width=40)

    for label in sorted(unused_labels, key=lambda x: x.name.lower()):
        table.add_row(label.name, label.id)

    console.print(table)
    console.print()

    if dry_run is True:
        console.print(f"[yellow]DRY RUN:[/yellow] Would delete {len(unused_labels)} label(s)")
        return

    if dry_run is None and not typer.confirm(f"Delete {len(unused_labels)} unused label(s)?"):
        console.print("[yellow]Cancelled[/yellow]")
        return

    deleted = 0
    failed = 0

    for label in unused_labels:
        try:
            client.delete_label(label.id)
            console.print(f"  [red]-[/red] Deleted: {label.name}")
            deleted += 1
        except Exception as e:
            console.print(f"  [red]✗[/red] Failed to delete {label.name}: {e}")
            failed += 1

    console.print(f"\n[green]✓[/green] Deleted {deleted} label(s)")
    if failed:
        console.print(f"[red]✗[/red] Failed to delete {failed} label(s)")
