"""Plan display and formatting functions."""

import contextlib
from collections import defaultdict
from collections.abc import Iterable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Protocol, runtime_checkable

from rich.console import Console
from rich.table import Table

from gmail_archiver.gmail_api_models import GmailMessageWithHeaders, SystemLabel
from gmail_archiver.inbox import GmailInbox
from gmail_archiver.plan import Plan, PlannedAction


@runtime_checkable
class Displayable(Protocol):
    """Protocol for custom_data models that can display in plan tables."""

    @classmethod
    def display_columns(cls) -> list[tuple[str, str]]:
        """Return list of (key, header_label) for table columns."""
        ...

    def format_column(self, key: str) -> str:
        """Format the value for a given column key."""
        ...

    @classmethod
    def hide_subject(cls) -> bool:
        """Return True to hide Subject column.

        Only return True when display_columns() fully captures all information
        from the email subject. The Subject column should be hidden ONLY when
        the custom columns provide complete structured representation of what
        the subject contained - hiding it must not lose information.

        Example: AliExpress subject "Order 12345: delivered" is fully captured
        by Order ID + Status columns, so hide_subject() returns True.
        """
        return False


def gmail_link(message_id: str) -> str:
    """Generate Rich markup hyperlink to Gmail web UI."""
    url = f"https://mail.google.com/mail/#all/{message_id}"
    return f"[link={url}]{message_id}[/link]"


def _collect_display_columns(actions: Iterable[PlannedAction]) -> tuple[list[tuple[str, str]], bool]:
    """Collect display columns and determine if Subject should be hidden.

    Returns (columns, hide_subject). Subject is hidden only if ALL Displayable items request it.
    """
    seen_keys: set[str] = set()
    columns: list[tuple[str, str]] = []
    hide_subject_votes: list[bool] = []

    for planned_action in actions:
        data = planned_action.action.custom_data
        if isinstance(data, Displayable):
            for key, label in data.display_columns():
                if key not in seen_keys:
                    seen_keys.add(key)
                    columns.append((key, label))
            hide_subject_votes.append(data.hide_subject())

    # Hide subject only if all Displayable items want it hidden (and there's at least one)
    hide_subject = bool(hide_subject_votes) and all(hide_subject_votes)
    return columns, hide_subject


def _create_table(title: str | None, custom_columns: list[tuple[str, str]], show_subject: bool = True) -> Table:
    """Build a Rich table with base and custom columns."""
    table = Table(title=title)
    table.add_column("Action", style="cyan")
    table.add_column("Gmail Link", style="blue", no_wrap=True)
    table.add_column("Date", style="magenta")
    if show_subject:
        table.add_column("Subject", style="green")

    for _key, label in custom_columns:
        table.add_column(label, style="yellow")

    return table


def _format_date(metadata: GmailMessageWithHeaders) -> str:
    """Render date as YYYY-MM-DD HH:MM using Date header or internal_date."""
    dt: datetime | None = None

    if metadata.date_header:
        with contextlib.suppress(TypeError, ValueError, OverflowError):  # malformed or unsupported dates
            dt = parsedate_to_datetime(metadata.date_header)

    if dt is None:
        with contextlib.suppress(TypeError, ValueError, OverflowError):  # missing/invalid internal_date
            millis = int(metadata.internal_date)
            dt = datetime.fromtimestamp(millis / 1000, tz=UTC)

    if dt is None:
        return ""

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)

    # Display in local time for readability
    local_dt = dt.astimezone()
    return local_dt.strftime("%Y-%m-%d %H:%M")


def display_plan(plan: Plan, inbox: GmailInbox, console: Console, dry_run: bool, group_by_category: bool = False):
    if not plan.actions:
        console.print("[yellow]No actions planned[/yellow]")
        return

    # Batch fetch metadata for all messages not already cached
    inbox.ensure_metadata_cached(plan.actions.keys())

    # When grouping by category on a merged plan, render one table per planner so
    # columns only appear when that planner's emails provide data for them.
    if group_by_category and plan.planner_name is None:
        by_planner: defaultdict[str, list[tuple[str, PlannedAction]]] = defaultdict(list)
        for message_id, planned_action in plan.actions.items():
            planner_name = planned_action.planner_name or "Unknown planner"
            by_planner[planner_name].append((message_id, planned_action))

        for planner_name in sorted(by_planner):
            items = by_planner[planner_name]
            custom_columns, hide_subject = _collect_display_columns(pa for _, pa in items)
            show_subject = not hide_subject
            table = _create_table(planner_name, custom_columns, show_subject=show_subject)

            for message_id, planned_action in items:
                _add_table_row(table, inbox, message_id, planned_action, dry_run, custom_columns, show_subject)

            console.print(table)
            console.print()
    else:
        # Single planner or explicit flat display
        custom_columns, hide_subject = _collect_display_columns(plan.actions.values())
        show_subject = not hide_subject
        table = _create_table("Inbox Cleanup - Action Plan", custom_columns, show_subject=show_subject)

        for message_id, planned_action in plan.actions.items():
            _add_table_row(table, inbox, message_id, planned_action, dry_run, custom_columns, show_subject)

        console.print(table)


def _add_table_row(
    table: Table,
    inbox: GmailInbox,
    message_id: str,
    planned_action: PlannedAction,
    dry_run: bool,
    custom_columns: list[tuple[str, str]],
    show_subject: bool = True,
):
    # Look up message metadata from inbox cache (fetches on demand if not cached)
    metadata = inbox.get_metadata(message_id)
    action = planned_action.action

    # Compute action icon
    has_ops = action.labels_to_add or action.labels_to_remove
    removes_inbox = SystemLabel.INBOX in action.labels_to_remove

    if not has_ops:
        action_icon = "📌 keep"
    elif removes_inbox:
        action_icon = "📦 would archive" if dry_run else "✓ archived"
    else:
        action_icon = "🏷️  label"

    # Format Gmail link (keep short label but clickable)
    link = gmail_link(message_id)

    # Format date from metadata
    date_str = _format_date(metadata)

    # Format custom data values using protocol
    custom_values = []
    data = action.custom_data
    for key, _label in custom_columns:
        if isinstance(data, Displayable):
            custom_values.append(data.format_column(key))
        else:
            custom_values.append("")

    if show_subject:
        subject = (metadata.subject or "")[:40]
        table.add_row(action_icon, link, date_str, subject, *custom_values)
    else:
        table.add_row(action_icon, link, date_str, *custom_values)


def summarize_plan(plan: Plan) -> str:
    total = len(plan.actions)

    # Count actual operations
    remove_inbox = sum(1 for p in plan.actions.values() if SystemLabel.INBOX in p.action.labels_to_remove)
    add_labels = sum(len(p.action.labels_to_add) for p in plan.actions.values())
    remove_labels = sum(len(p.action.labels_to_remove) for p in plan.actions.values())
    no_op = sum(1 for p in plan.actions.values() if not p.action.labels_to_add and not p.action.labels_to_remove)

    parts = [f"Total: {total}"]
    if remove_inbox > 0:
        parts.append(f"Remove from inbox: {remove_inbox}")
    if add_labels > 0:
        parts.append(f"Add labels: {add_labels}")
    if remove_labels > 0:
        parts.append(f"Remove labels: {remove_labels}")
    if no_op > 0:
        parts.append(f"No-op: {no_op}")

    return ", ".join(parts)
