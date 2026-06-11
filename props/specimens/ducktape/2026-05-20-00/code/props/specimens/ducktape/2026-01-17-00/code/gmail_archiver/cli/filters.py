"""Gmail filters subcommands."""

import io
import itertools
from pathlib import Path
from typing import Annotated

import typer
import yaml
from rich.console import Console
from rich.table import Table

from gmail_archiver.cli.common import DryRunOption, TokenFileOption, get_client
from gmail_archiver.dirs import get_cache_dir
from gmail_archiver.filter_planner import GmailFilterPlanner
from gmail_archiver.filter_sync import (
    FilterDiff,
    LabelMaps,
    NormalizedFilter,
    diff_filters,
    format_filter_for_display,
    normalize_gmail_filter,
    normalize_yaml_rule,
    normalized_to_create_request,
)
from gmail_archiver.gmail_api_models import GmailFilter, SystemLabel, is_system_label
from gmail_archiver.gmail_client import GmailClient
from gmail_archiver.gmail_yaml_filters_models import FilterRule, FilterRuleSet, ForEachRule
from gmail_archiver.inbox import GmailInbox
from gmail_archiver.plan_display import display_plan, summarize_plan

filters_app = typer.Typer(help="Manage Gmail filters")

FiltersFileArg = Annotated[Path, typer.Argument(help="Path to filters YAML file")]


def load_yaml_filters(filters_file: Path, console: Console) -> list[NormalizedFilter]:
    """Load and normalize filters from YAML file."""
    if not filters_file.exists():
        console.print(f"[red]Error:[/red] File not found: {filters_file}")
        raise typer.Exit(code=1)

    yaml_list = yaml.safe_load(filters_file.read_text())
    if not yaml_list:
        return []

    ruleset = FilterRuleSet.from_yaml_list(yaml_list)
    normalized = []
    for rule in ruleset.rules:
        if isinstance(rule, FilterRule) and not rule.ignore:
            normalized.append(normalize_yaml_rule(rule))
    return normalized


def load_gmail_filters(client: GmailClient) -> tuple[list[NormalizedFilter], LabelMaps]:
    """Load and normalize filters from Gmail."""
    labels = client.list_labels_full()
    label_maps = LabelMaps.from_labels(labels)
    api_filters = client.list_filters()
    normalized = [normalize_gmail_filter(f, label_maps.by_id) for f in api_filters]
    return normalized, label_maps


def display_diff(diff: FilterDiff, console: Console) -> None:
    """Display filter diff in a nice table."""
    if not diff.to_create and not diff.to_delete:
        console.print("[green]✓[/green] Filters are in sync. No changes needed.")
        return

    table = Table(title="Filter Diff")
    table.add_column("Action", style="cyan", width=10)
    table.add_column("Filter")

    for f in diff.to_create:
        table.add_row("[green]+ create[/green]", format_filter_for_display(f))

    for f in diff.to_delete:
        table.add_row("[red]- delete[/red]", format_filter_for_display(f))

    console.print(table)
    console.print()
    console.print(
        f"Summary: [green]+{len(diff.to_create)}[/green] to create, [red]-{len(diff.to_delete)}[/red] to delete, {len(diff.unchanged)} unchanged"
    )


def prompt_and_execute(dry_run: bool | None, description: str, count: int, execute_fn, console: Console) -> bool:
    """Handle dry-run/interactive/immediate execution pattern.

    Returns True if executed, False if skipped/cancelled.
    """
    if count == 0:
        return False

    if dry_run is True:
        console.print(f"[yellow]DRY RUN:[/yellow] Would {description}")
        return False

    if dry_run is None and not typer.confirm(f"{description}?"):  # Interactive
        console.print("[yellow]Cancelled[/yellow]")
        return False

    execute_fn()
    return True


@filters_app.command("download")
def download(
    output: Annotated[Path, typer.Option("--output", "-o", help="Output YAML file path")] = Path(
        "filters_exported.yaml"
    ),
    token_file: TokenFileOption = None,
):
    """Download Gmail filters to YAML file."""
    console = Console()
    console.print("Connecting to Gmail...")
    client = get_client(token_file)

    console.print("Fetching labels and filters...")
    label_maps = LabelMaps.from_labels(client.list_labels_full())
    api_filters = client.list_filters()

    if not api_filters:
        console.print("[yellow]No filters found.[/yellow]")
        return

    console.print(f"Found {len(api_filters)} filters.")

    filter_rules: list[FilterRule | ForEachRule] = [_gmail_filter_to_rule(f, label_maps.by_id) for f in api_filters]

    yaml_list = FilterRuleSet(rules=filter_rules).to_yaml_list()
    buffer = io.StringIO()
    buffer.write("# Gmail filters exported by gmail-archiver\n")
    buffer.write("#\n\n")
    yaml.dump(yaml_list, buffer, default_flow_style=False, allow_unicode=True, sort_keys=False)
    output.write_text(buffer.getvalue())

    console.print(f"\n[green]✓[/green] Exported {len(filter_rules)} filters to {output}")


def _gmail_filter_to_rule(gmail_filter: GmailFilter, labels_by_id: dict[str, str]) -> FilterRule:
    """Convert GmailFilter to FilterRule for YAML export."""
    criteria = gmail_filter.criteria
    action = gmail_filter.action

    add_label_ids = action.add_label_ids
    remove_label_ids = action.remove_label_ids

    label = None
    for label_id in add_label_ids:
        if label_id in labels_by_id and not is_system_label(label_id):
            label = labels_by_id[label_id]
            break

    # Use dict unpacking to handle keyword conflict with "from"
    return FilterRule(
        **{
            "from": criteria.from_,
            "to": criteria.to,
            "subject": criteria.subject,
            "has": criteria.query,
            "does_not_have": criteria.negated_query,
            "label": label,
            "important": True if SystemLabel.IMPORTANT in add_label_ids else None,
            "star": True if SystemLabel.STARRED in add_label_ids else None,
            "trash": True if SystemLabel.TRASH in add_label_ids else None,
            "archive": True if SystemLabel.INBOX in remove_label_ids else None,
            "read": True if SystemLabel.UNREAD in remove_label_ids else None,
            "not_important": True if SystemLabel.IMPORTANT in remove_label_ids else None,
            "not_spam": True if SystemLabel.SPAM in remove_label_ids else None,
            "forward": action.forward,
        }
    )


@filters_app.command("diff")
def diff(filters_file: FiltersFileArg, token_file: TokenFileOption = None):
    """Show diff between YAML and Gmail filters (read-only)."""
    console = Console()
    console.print("Loading filters...")

    yaml_filters = load_yaml_filters(filters_file, console)
    console.print(f"  YAML: {len(yaml_filters)} filters")

    client = get_client(token_file)
    gmail_filters, _ = load_gmail_filters(client)
    console.print(f"  Gmail: {len(gmail_filters)} filters")
    console.print()

    display_diff(diff_filters(yaml_filters, gmail_filters), console)


@filters_app.command("upload")
def upload(filters_file: FiltersFileArg, dry_run: DryRunOption = None, token_file: TokenFileOption = None):
    """Upload filters from YAML to Gmail (creates only, skips existing)."""
    console = Console()
    console.print("Loading filters...")

    yaml_filters = load_yaml_filters(filters_file, console)
    console.print(f"  YAML: {len(yaml_filters)} filters")

    client = get_client(token_file)
    gmail_filters, label_maps = load_gmail_filters(client)
    console.print(f"  Gmail: {len(gmail_filters)} filters")
    console.print()

    filter_diff = diff_filters(yaml_filters, gmail_filters)

    if not filter_diff.to_create:
        console.print("[green]✓[/green] All filters already exist. Nothing to upload.")
        return

    # Show what will be created
    console.print(f"[bold]Filters to create ({len(filter_diff.to_create)}):[/bold]")
    for f in filter_diff.to_create:
        console.print(f"  [green]+[/green] {format_filter_for_display(f)}")
    console.print()

    def do_upload():
        created = 0
        for f in filter_diff.to_create:
            try:
                for label_name in f.add_labels:
                    label_maps.ensure_label(label_name, client.get_or_create_label)

                client.create_filter(normalized_to_create_request(f, label_maps.by_name))
                created += 1
                console.print(f"  [green]✓[/green] Created: {format_filter_for_display(f)}")
            except Exception as e:
                console.print(f"  [red]✗[/red] Failed: {format_filter_for_display(f)}: {e}")

        console.print(f"\n[green]✓[/green] Created {created} filter(s)")

    prompt_and_execute(
        dry_run, f"Create {len(filter_diff.to_create)} filter(s)", len(filter_diff.to_create), do_upload, console
    )


@filters_app.command("sync")
def sync(filters_file: FiltersFileArg, dry_run: DryRunOption = None, token_file: TokenFileOption = None):
    """Sync Gmail filters with YAML (create missing, delete extras)."""
    console = Console()
    console.print("Loading filters...")

    yaml_filters = load_yaml_filters(filters_file, console)
    console.print(f"  YAML: {len(yaml_filters)} filters")

    client = get_client(token_file)
    gmail_filters, label_maps = load_gmail_filters(client)
    console.print(f"  Gmail: {len(gmail_filters)} filters")
    console.print()

    filter_diff = diff_filters(yaml_filters, gmail_filters)
    display_diff(filter_diff, console)

    if not filter_diff.to_create and not filter_diff.to_delete:
        return

    console.print()

    def do_sync():
        if filter_diff.to_delete:
            console.print(f"\n[bold]Deleting {len(filter_diff.to_delete)} filter(s)...[/bold]")
            for f in filter_diff.to_delete:
                try:
                    if f.id:
                        client.delete_filter(f.id)
                        console.print(f"  [red]-[/red] Deleted: {format_filter_for_display(f)}")
                except Exception as e:
                    console.print(f"  [red]✗[/red] Failed to delete: {e}")

        if filter_diff.to_create:
            console.print(f"\n[bold]Creating {len(filter_diff.to_create)} filter(s)...[/bold]")
            for f in filter_diff.to_create:
                try:
                    for label_name in f.add_labels:
                        label_maps.ensure_label(label_name, client.get_or_create_label)

                    client.create_filter(normalized_to_create_request(f, label_maps.by_name))
                    console.print(f"  [green]+[/green] Created: {format_filter_for_display(f)}")
                except Exception as e:
                    console.print(f"  [red]✗[/red] Failed to create: {e}")

        console.print("\n[green]✓[/green] Sync complete")

    total_changes = len(filter_diff.to_create) + len(filter_diff.to_delete)
    prompt_and_execute(
        dry_run,
        f"Apply {total_changes} change(s) ({len(filter_diff.to_create)} create, {len(filter_diff.to_delete)} delete)",
        total_changes,
        do_sync,
        console,
    )


@filters_app.command("list")
def list_filters(token_file: TokenFileOption = None):
    """List all Gmail filters from the server with their IDs."""
    console = Console()
    console.print("Fetching filters from Gmail...")
    client = get_client(token_file)
    gmail_filters, _label_maps = load_gmail_filters(client)

    if not gmail_filters:
        console.print("[yellow]No filters found.[/yellow]")
        return

    table = Table(title="Gmail Filters")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Filter")

    for f in gmail_filters:
        table.add_row(f.id or "", format_filter_for_display(f))

    console.print(table)
    console.print(f"\n{len(gmail_filters)} filter(s) total")


@filters_app.command("apply")
def apply(
    filter_id: Annotated[str, typer.Argument(help="Gmail filter ID (from 'filters list' command)")],
    query: Annotated[str | None, typer.Option("--query", "-q", help="Additional Gmail query to narrow scope")] = None,
    dry_run: DryRunOption = None,
    token_file: TokenFileOption = None,
):
    """Apply a Gmail filter to existing emails matching its criteria.

    Use 'gmail-archiver filters list' to see available filter IDs.
    """
    console = Console()
    console.print("Fetching filter from Gmail...")
    client = get_client(token_file)

    api_filters = client.list_filters()
    target_filter = None
    for f in api_filters:
        if f.id == filter_id:
            target_filter = f
            break

    if not target_filter:
        console.print(f"[red]Error:[/red] Filter not found: {filter_id}")
        console.print("Use 'gmail-archiver filters list' to see available filters.")
        raise typer.Exit(code=1)

    labels = client.list_labels_full()
    label_maps = LabelMaps.from_labels(labels)

    planner = GmailFilterPlanner(target_filter, label_maps.by_id, additional_query=query)
    console.print(f"Filter: {planner.name}")

    inbox = GmailInbox(client, console, cache_dir=get_cache_dir())
    plan = planner.plan(inbox)

    for msg in plan.messages:
        console.print(f"[dim]{msg}[/dim]")

    ops_count = plan.count_operations()

    if ops_count == 0:
        console.print("[yellow]All emails already have the correct labels. Nothing to do.[/yellow]")
        return

    delta_summary = plan.format_delta_summary(label_maps.by_id)

    display_plan(plan, inbox, console=console, dry_run=dry_run is True)

    def do_apply():
        total_processed = 0
        for sig, msg_ids in plan.group_by_signature().items():
            batch_size = 1000
            for batch in itertools.batched(msg_ids, batch_size, strict=False):
                body: dict = {"ids": list(batch)}
                if sig.labels_to_add:
                    body["addLabelIds"] = list(sig.labels_to_add)
                if sig.labels_to_remove:
                    body["removeLabelIds"] = list(sig.labels_to_remove)

                client.service.users().messages().batchModify(userId="me", body=body).execute()
                total_processed += len(batch)

        console.print(f"[green]✓[/green] Applied filter to {total_processed} email(s)")

    prompt_and_execute(dry_run, f"Apply to {ops_count} email(s) ({delta_summary})", ops_count, do_apply, console)
    console.print(f"\n{summarize_plan(plan)}")
