"""Display utilities for props CLI commands."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any, Literal, Protocol, TypeVar
from uuid import UUID

from rich import box
from rich.console import Console
from rich.table import Table

from props.db.models import AgentRunStatus, StatsWithCI

# Display constants
SHORT_UUID_LENGTH = 8
SHORT_SHA_LENGTH = 6

JustifyMethod = Literal["left", "right", "center", "full", "default"]
T = TypeVar("T")
V = TypeVar("V")


def short_uuid(uuid: UUID) -> str:
    """Return first 8 characters of UUID for display."""
    return str(uuid)[:SHORT_UUID_LENGTH]


def short_sha(sha: str) -> str:
    """Return first 6 characters of SHA hash for display."""
    return sha[:SHORT_SHA_LENGTH]


def fmt_pct(value: float | None) -> str:
    """Format value as percentage (1 decimal place) or dash if None."""
    return f"{value:.1%}" if value is not None else "—"


# ============================================================================
# Status Count Columns
# ============================================================================


class HasStatusCounts(Protocol):
    """Row with status_counts dict (ORM models or raw SQL rows)."""

    @property
    def status_counts(self) -> dict[str, int] | None: ...


# Status display mapping: AgentRunStatus -> (header, column width)
# IN_PROGRESS excluded since it's transient, not a terminal outcome
STATUS_DISPLAY: dict[AgentRunStatus, tuple[str, int]] = {
    AgentRunStatus.COMPLETED: ("✓", 4),
    AgentRunStatus.MAX_TURNS_EXCEEDED: ("S", 3),
    AgentRunStatus.CONTEXT_LENGTH_EXCEEDED: ("C", 3),
    AgentRunStatus.REPORTED_FAILURE: ("F", 3),
}


def get_status_count(status: AgentRunStatus, row: HasStatusCounts) -> int:
    """Get count for a specific status from row.status_counts dict."""
    if row.status_counts is None:
        return 0
    # Handle both string keys (from raw SQL) and enum keys (from ORM)
    count = row.status_counts.get(status) or row.status_counts.get(status.value, 0)
    return count or 0


def _make_status_column(status: AgentRunStatus) -> ColumnDef[HasStatusCounts, int]:
    """Create a column definition for a status count."""
    header, width = STATUS_DISPLAY[status]
    return ColumnDef(header, partial(get_status_count, status), str, justify="right", width=width)


def build_status_columns() -> list[ColumnDef[HasStatusCounts, int]]:
    """Build list of status count columns (✓, S, C, F)."""
    return [_make_status_column(status) for status in STATUS_DISPLAY]


# ============================================================================
# Recall Stats Columns (stats_with_ci)
# ============================================================================


class HasRecallStats(Protocol):
    """Row with recall_stats (StatsWithCI | None)."""

    @property
    def recall_stats(self) -> StatsWithCI | None: ...


def get_recall_mean(row: HasRecallStats) -> float | None:
    """Extract mean recall from row.recall_stats."""
    return row.recall_stats.mean if row.recall_stats else None


def get_recall_ucb(row: HasRecallStats) -> float | None:
    """Extract UCB95 from row.recall_stats."""
    return row.recall_stats.ucb95 if row.recall_stats else None


def get_recall_lcb(row: HasRecallStats) -> float | None:
    """Extract LCB95 from row.recall_stats."""
    return row.recall_stats.lcb95 if row.recall_stats else None


def build_recall_columns() -> list[ColumnDef[HasRecallStats, float | None]]:
    """Build list of recall stats columns (Recall, UCB, LCB)."""
    return [
        ColumnDef(name, accessor, fmt_pct, justify="right", width=7)
        for name, accessor in [("Recall", get_recall_mean), ("UCB", get_recall_ucb), ("LCB", get_recall_lcb)]
    ]


def ellipticize(text: str, max_len: int) -> str:
    """Truncate text with "...(N more)" suffix if it exceeds max_len."""
    if len(text) <= max_len:
        return text
    remaining = len(text) - max_len
    return f"{text[:max_len]}...({remaining} more)"


def format_truncation_footer(total_count: int, displayed_count: int, item_name: str = "items") -> str:
    """Return "... (N more {item_name})" or empty string if all shown."""
    if total_count > displayed_count:
        remaining = total_count - displayed_count
        return f"... ({remaining} more {item_name})"
    return ""


def print_truncation_footer(
    print_fn: Any, total_count: int, displayed_count: int, item_name: str = "items", prefix: str = "\n"
) -> None:
    """Print truncation footer via print_fn if items were truncated."""
    if footer := format_truncation_footer(total_count, displayed_count, item_name):
        print_fn(f"{prefix}{footer}")


@dataclass
class ColumnDef[T, V]:
    """Column definition for declarative table building."""

    name: str
    accessor: Callable[[T], V]
    formatter: Callable[[V], str] = str
    width: int | None = None
    justify: JustifyMethod = "left"
    style: str | None = None


def build_table_from_schema[T](
    rows: Sequence[T],
    columns: Sequence[ColumnDef[T, Any]],
    *,
    show_header: bool = True,
    box_style: box.Box = box.SIMPLE,
) -> Table:
    """Build a Rich table from column schema and data rows."""
    table = Table(show_header=show_header, header_style="bold cyan", box=box_style)

    # Add columns from schema
    for col in columns:
        table.add_column(col.name, width=col.width, justify=col.justify, style=col.style)

    # Add rows
    for row in rows:
        values = [col.formatter(col.accessor(row)) for col in columns]
        table.add_row(*values)

    return table


def print_table_with_footer[T](
    console: Console,
    rows: Sequence[T],
    columns: Sequence[ColumnDef[T, Any]],
    *,
    show_header: bool = True,
    box_style: box.Box = box.SIMPLE,
    total_count: int | None = None,
    item_name: str = "items",
) -> None:
    """Build and print a table, appending truncation footer if total_count provided."""
    table = build_table_from_schema(rows, columns, show_header=show_header, box_style=box_style)
    console.print(table)
    if total_count is not None:
        print_truncation_footer(console.print, total_count, len(rows), item_name)
