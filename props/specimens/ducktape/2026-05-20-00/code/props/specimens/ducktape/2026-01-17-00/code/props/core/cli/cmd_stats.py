"""CLI command for prompt statistics and evaluation metrics."""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import plotext as plt
import typer
from rich import box
from rich.console import Console
from rich.table import Table
from sqlalchemy import func

from props.core.agent_types import AgentType, CriticTypeConfig
from props.core.db.examples import count_available_examples_by_scope_all, count_available_examples_for_split
from props.core.db.models import (
    AgentDefinition,
    AgentRun,
    AgentRunStatus,
    Event,
    OccurrenceCredit,
    OccurrenceStatistics,
    RecallByDefinitionSplitKind,
    RecallByExample,
    Snapshot,
)
from props.core.db.query_builders import query_recall_by_example
from props.core.db.session import get_session
from props.core.display import (
    SHORT_SHA_LENGTH,
    ColumnDef,
    build_status_columns,
    build_table_from_schema,
    fmt_pct,
    short_sha,
)
from props.core.grader.staleness import identify_stale_runs
from props.core.models.examples import ExampleKind, ExampleSpec, SingleFileSetExample, WholeSnapshotExample
from props.core.splits import Split

# Stats subcommand group
stats_app = typer.Typer(help="Statistics and metrics commands")


# ============================================================================
# Formatting Helpers
# ============================================================================


def fmt_float(value: float | None, decimals: int = 2) -> str:
    """Format float with N decimal places or dash if None."""
    return f"{value:.{decimals}f}" if value is not None else "—"


def fmt_model(model: str, max_length: int = 12) -> str:
    """Truncate model name to max_length characters."""
    return model[:max_length]


def fmt_hash(hash_value: str | None) -> str:
    """Format hash as short SHA prefix or dash if None."""
    return short_sha(hash_value) if hash_value else "—"


# Common dimension columns (used by prompt and example stats)
_DIMENSION_COLUMNS: list[ColumnDef[Any, Any]] = [
    ColumnDef("Split", lambda r: r.split, width=6),
    ColumnDef("Critic", lambda r: r.critic_model, fmt_model, width=12),
]

# Status columns - built from shared display helpers
_STATUS_COLUMNS: list[ColumnDef[Any, Any]] = build_status_columns()

# Common occurrence-based columns (used by prompt and example stats)
# Views now use:
#   - recall_stats: StatsWithCI with 0..1 recall ratios (pre-scaled via scale_stats() in VIEW)
#   - credit_stats: StatsWithCI with raw credit counts
#   - recall_denominator: int
#   - status_counts: dict[AgentRunStatus, int]
_OCCURRENCE_COLUMNS: list[ColumnDef[Any, Any]] = [
    ColumnDef("Recall %", lambda r: r.recall_stats.mean if r.recall_stats else 0.0, fmt_pct, justify="right", width=9),
    ColumnDef(
        "Credit",
        lambda r: r.credit_stats.mean if r.credit_stats else 0.0,
        lambda v: fmt_float(v, decimals=1),
        justify="right",
        width=7,
    ),
    ColumnDef(
        "Catchable", lambda r: r.recall_denominator, lambda v: fmt_float(v, decimals=1), justify="right", width=9
    ),
    *_STATUS_COLUMNS,
]


# Stats table column legend (shared between CLI and examples)
STATS_TABLE_LEGEND = """
[bold]Column Legend:[/bold]
  [cyan]Recall%[/cyan]: Mean recall over all runs (failures count as 0.0)
  [cyan]LCB[/cyan]: Lower confidence bound (mean - 1σ/√n), — if n < 2
    - Penalizes high variance, useful for selecting reliable prompts
    - ~84% confidence the true mean is above this value
  [cyan]N / {total}[/cyan]: Number of examples evaluated out of total available
  [cyan]Z[/cyan]: Count of examples with 0% recall
  [cyan]✓[/cyan]: Count of completed runs
  [cyan]S[/cyan]: Count of runs that exceeded max turns (stuck)
  [cyan]C[/cyan]: Count of runs that exceeded context length
  [cyan]F[/cyan]: Count of runs that reported failure

[bold]Split Groups:[/bold]
  [cyan]Valid[/cyan]: Validation split (whole-snapshot only, terminal metric)
  [yellow]Tr-W[/yellow]: Train whole-snapshot (comprehensive full-repo review)
  [magenta]Tr-P[/magenta]: Train partial (file-set examples)

[bold]Notes:[/bold]
  - Results sorted by valid LCB, then train-whole LCB, then train-partial LCB, then age
  - Status columns show count distribution (✓=success, S=stuck, C=context, F=failure)
  - Green recall means fully evaluated (N = total available)
  - Many prompts have no valid data (— in Valid columns)
"""


def format_age(dt: datetime) -> str:
    """Format datetime as relative age string (e.g., '2d', '3h', '15m').

    Args:
        dt: Datetime to format (assumed UTC or naive)

    Returns:
        Age string like '2y', '3mo', '5d', '12h', '45m', or 'now'
    """
    now = datetime.now(UTC) if dt.tzinfo else datetime.now()
    delta = now - dt

    if delta.days >= 365:
        return f"{delta.days // 365}y"
    if delta.days >= 30:
        return f"{delta.days // 30}mo"
    if delta.days > 0:
        return f"{delta.days}d"
    if delta.seconds >= 3600:
        return f"{delta.seconds // 3600}h"
    if delta.seconds >= 60:
        return f"{delta.seconds // 60}m"
    return "now"


def _generate_buckets(
    values: Sequence[float | int], num_buckets: int = 7, equal_width: bool = False
) -> list[tuple[str, float | int, float | int]]:
    """Generate bucket ranges automatically based on data distribution.

    Args:
        values: List of numeric values
        num_buckets: Desired number of buckets (default 7)
        equal_width: If True, use equal-width bins; if False, use percentile-based (default False)

    Returns:
        List of (label, low, high) tuples defining bucket ranges
    """
    if not values:
        return []

    min_val = min(values)
    max_val = max(values)

    # If all values are the same, create a single bucket
    if min_val == max_val:
        return [(str(min_val), min_val, min_val + 1)]

    if equal_width:
        # Use equal-width bins
        bin_width = (max_val - min_val) / num_buckets
        unique_bounds = [min_val + i * bin_width for i in range(num_buckets + 1)]
    else:
        # Use percentile-based buckets for better distribution
        sorted_vals = sorted(values)
        n = len(sorted_vals)

        # Calculate bucket boundaries using percentiles
        percentiles = [i * 100 / num_buckets for i in range(num_buckets + 1)]
        bounds = []
        for p in percentiles:
            idx = int(n * p / 100)
            if idx >= n:
                idx = n - 1
            bounds.append(sorted_vals[idx])

        # Deduplicate consecutive boundaries
        unique_bounds = [bounds[0]]
        for b in bounds[1:]:
            if b != unique_bounds[-1]:
                unique_bounds.append(b)

        # If we have fewer unique bounds than buckets, fall back to equal-width bins
        if len(unique_bounds) < num_buckets:
            bin_width = (max_val - min_val) / num_buckets
            unique_bounds = [min_val + i * bin_width for i in range(num_buckets + 1)]

    # Create bucket tuples with labels
    buckets = []
    for i in range(len(unique_bounds) - 1):
        low = unique_bounds[i]
        high = unique_bounds[i + 1]

        # Format label based on value type and range
        if isinstance(low, int) and isinstance(high, int):
            label = str(low) if low == high - 1 else f"{low}-{high - 1}"
        else:
            label = f"{low:.1f}-{high:.1f}"

        # Adjust high boundary to be exclusive for the last bucket
        if i == len(unique_bounds) - 2:
            high = max_val + 1  # Make last bucket inclusive

        buckets.append((label, low, high))

    return buckets


def _display_distribution(
    console: Console,
    values: Sequence[float | int],
    title: str,
    buckets: Sequence[tuple[str, float | int, float | int]],
    value_format: str = "{:.1f}%",
) -> None:
    """Display a bucketed distribution with percentiles and histogram using plotext.

    Args:
        console: Rich console for output
        values: List of numeric values to visualize
        title: Section title
        buckets: List of (label, low, high) tuples defining bucket ranges
        value_format: Format string for displaying values (default: percentage)
    """
    if not values:
        return

    # Convert to list for statistics functions
    values_list = list(values)
    mean_val = statistics.mean(values_list)
    median_val = statistics.median(values_list)

    console.print(f"\n[bold]{title}:[/bold]")
    console.print(f"  N={len(values)} μ={value_format.format(mean_val)} median={value_format.format(median_val)}")

    # Show percentiles for skewed distributions on one line
    sorted_vals = sorted(values_list)
    n = len(sorted_vals)
    percentiles = [10, 25, 50, 75, 90, 95, 99]
    percentile_strs = []
    for p in percentiles:
        idx = int(n * p / 100)
        if idx >= n:
            idx = n - 1
        percentile_strs.append(f"P{p}={value_format.format(sorted_vals[idx])}")
    console.print(f"  Percentiles: {' '.join(percentile_strs)}")

    # Create histogram using plotext
    # Compute bucket counts
    bucket_labels = [label for label, _, _ in buckets]
    bucket_counts = [sum(1 for v in values_list if low <= v < high) for _, low, high in buckets]

    # Use plotext for simple horizontal bar chart with default colors
    plt.clear_figure()
    plt.clear_color()  # Reset to default colors
    plt.simple_bar(bucket_labels, bucket_counts, width=50, title="")
    plt.show()
    console.print()


@dataclass
class SplitStats:
    """Statistics for a prompt on a specific split."""

    completed: int = 0  # Unique examples evaluated (with grader runs completed)
    recalls: list[float] = None  # type: ignore[assignment]
    total_available: int = 0  # Total training examples available in this split
    critic_max_turns: int = 0  # Number of critic runs that exceeded max turns

    def __post_init__(self) -> None:
        if self.recalls is None:
            self.recalls = []

    @property
    def mean_recall(self) -> float | None:
        """Mean recall percentage (0-100) or None if no data."""
        if not self.recalls:
            return None
        return statistics.mean(self.recalls)

    @property
    def zero_rate(self) -> float | None:
        """Percentage of samples with 0% recall, or None if no data."""
        if not self.recalls:
            return None
        zeros = sum(1 for r in self.recalls if r == 0.0)
        return 100.0 * zeros / len(self.recalls)


@dataclass
class PromptStats:
    """Statistics for a single prompt across all splits."""

    prompt_sha256: str
    created_at: datetime
    splits: dict[Split, SplitStats]
    valid_best_count: int = 0  # Number of valid samples where this prompt is best (or tied)


def _display_split_analysis(
    console: Console,
    split_name: str,
    sample_results: dict[ExampleSpec, dict[str, float]],
    tp_counts_per_sample: dict[ExampleSpec, int],
    total_available: int,
    show_all_prompts: bool = False,
) -> None:
    """Display analysis of which prompts are best on which examples for a split.

    Args:
        console: Rich console for output
        split_name: Name of the split (e.g., "Training", "Validation")
        sample_results: Dict mapping ExampleSpec -> {prompt_sha: recall_pct}
        tp_counts_per_sample: Dict mapping ExampleSpec -> TP count
        total_available: Total number of examples available in this split
        show_all_prompts: If True, show all prompts instead of top 15
    """
    num_evaluated = len(sample_results)
    num_unknown = total_available - num_evaluated

    console.print(f"\n[bold]{split_name} Split Analysis[/bold] ({total_available} examples total)\n")

    # Categorize examples by best recall
    zero_recall_samples = []
    nonzero_recall_samples = []
    evaluated_sample_keys = set()  # Track which samples have been evaluated
    for sample_key, recalls in sample_results.items():
        evaluated_sample_keys.add(sample_key)
        max_recall = max(recalls.values())
        if max_recall == 0:
            zero_recall_samples.append((sample_key, recalls))
        else:
            nonzero_recall_samples.append((sample_key, recalls, max_recall))

    console.print(f"  Examples evaluated: {num_evaluated}")
    console.print(f"  Examples with best recall = 0: {len(zero_recall_samples)}")
    console.print(f"  Examples with best recall > 0: {len(nonzero_recall_samples)}")
    console.print(f"  [dim]Examples not evaluated (unknown): {num_unknown}[/dim]")

    # For zero-recall examples, show which prompts have been tried most
    if zero_recall_samples:
        console.print(f"\n[bold]Zero-Recall Examples ({split_name}):[/bold]")
        prompt_counts_zero: Counter[str] = Counter()
        for _, recalls in zero_recall_samples:
            prompt_counts_zero.update(recalls.keys())

        # Show top prompts tried on zero-recall examples
        for sha, count in prompt_counts_zero.most_common(10):
            pct = 100.0 * count / len(zero_recall_samples)
            console.print(f"  {short_sha(sha)}: evaluated on {count}/{len(zero_recall_samples)} ({pct:.0f}%)")

    # For nonzero-recall examples, show prompt best-coverage heatmap
    if nonzero_recall_samples:
        console.print(f"\n[bold]Nonzero-Recall Examples ({split_name} - Prompt Best Coverage):[/bold]")

        # Build matrix: for each prompt, which examples is it best on?
        # Also track which prompts have evaluated which examples
        prompt_best_on = defaultdict(list)  # sha -> [(example_idx, recall, tp_count)]
        prompt_evaluated_on = defaultdict(set)  # sha -> set of example indices evaluated

        for idx, (sample_key, recalls, max_recall) in enumerate(nonzero_recall_samples):
            tp_count = tp_counts_per_sample.get(sample_key, 0)
            for sha, recall in recalls.items():
                prompt_evaluated_on[sha].add(idx)
                if recall == max_recall:  # This prompt is best (or tied) on this example
                    prompt_best_on[sha].append((idx, recall, tp_count))

        # Sort prompts by number of examples they're best on
        sorted_prompts = sorted(
            prompt_best_on.items(), key=lambda x: (len(x[1]), sum(r for _, r, _ in x[1])), reverse=True
        )

        # Create table
        # Show one character per example (not bucketed) since we have relatively few
        coverage_width = len(nonzero_recall_samples) + 2  # +2 for brackets
        coverage_table = Table(
            show_header=True, header_style="bold cyan", box=box.SIMPLE, show_edge=False, padding=(0, 1)
        )
        coverage_table.add_column("Prompt", style="dim", width=SHORT_SHA_LENGTH)
        coverage_table.add_column("Best On", justify="right", width=7)
        coverage_table.add_column("Pct", justify="right", width=5)
        coverage_table.add_column("Coverage", width=coverage_width)

        # Display each prompt's coverage
        prompts_to_show = sorted_prompts if show_all_prompts else sorted_prompts[:15]
        for sha, best_examples in prompts_to_show:
            count = len(best_examples)
            pct = 100.0 * count / len(nonzero_recall_samples)

            # Create visual bar: one character per example
            # Three states: not evaluated (' '), evaluated but not best ('░'), best ('▓')
            best_example_indices = {idx for idx, _, _ in best_examples}
            evaluated_indices = prompt_evaluated_on.get(sha, set())
            visual_chars = []
            for idx in range(len(nonzero_recall_samples)):
                if idx in best_example_indices:
                    visual_chars.append("▓")
                elif idx in evaluated_indices:
                    visual_chars.append("░")
                else:
                    visual_chars.append(" ")
            visual = "".join(visual_chars)

            coverage_table.add_row(
                short_sha(sha), f"{count}/{len(nonzero_recall_samples)}", f"{pct:.0f}%", f"[{visual}]"
            )

        console.print(coverage_table)


def _add_split_columns(table: Table, split_name: str, color: str, total_examples: int) -> None:
    """Add columns for a single split (Valid or Train) with consistent formatting.

    Args:
        table: Rich Table to add columns to
        split_name: Name of split (e.g., "Valid", "Train")
        color: Rich color name (e.g., "cyan", "yellow")
        total_examples: Total available examples for this split
    """
    # Column order: Recall, LCB, N/{total}, Z, ✓, S, C, F
    table.add_column(f"[{color}]{split_name} Recall[/{color}]", justify="right", width=11)
    table.add_column(f"[{color}]LCB[/{color}]", justify="right", width=7)
    table.add_column(f"[{color}]N/{total_examples}[/{color}]", justify="right", width=4)
    table.add_column(f"[{color}]Z[/{color}]", justify="right", width=4)
    table.add_column(f"[{color}]✓[/{color}]", justify="right", width=4)
    table.add_column(f"[{color}]S[/{color}]", justify="right", width=4)
    table.add_column(f"[{color}]C[/{color}]", justify="right", width=4)
    table.add_column(f"[{color}]F[/{color}]", justify="right", width=4)


@stats_app.command("critic-leaderboard")
def cmd_stats_critic_leaderboard(
    split: Split | None = None,
    critic_model: str | None = None,
    example_kind: ExampleKind | None = None,
    top: int | None = typer.Option(None, help="Show top N results by recall"),
    bottom: int | None = typer.Option(None, help="Show bottom N results by recall"),
) -> None:
    """Query aggregated recall metrics by agent definition.

    Shows recall metrics aggregated by (split, agent_definition_id, critic_model, example_kind).
    Aggregates over all grader models (occurrence-based weighting).
    By default shows top 50 results. Use --top/--bottom to customize.
    """
    console = Console()

    # Default to showing top 50 if neither top nor bottom is specified
    if top is None and bottom is None:
        top = 50

    with get_session() as session:
        # Build base query with filters
        base_query = session.query(RecallByDefinitionSplitKind)
        if split:
            base_query = base_query.filter(RecallByDefinitionSplitKind.split == split)
        if critic_model:
            base_query = base_query.filter(RecallByDefinitionSplitKind.critic_model == critic_model)
        if example_kind:
            base_query = base_query.filter(RecallByDefinitionSplitKind.example_kind == example_kind)

        # Fetch top and/or bottom results
        sections_to_show: list[tuple[str, list[RecallByDefinitionSplitKind]]] = []

        # TODO: Move sorting to SQL side using (recall_stats).mean for efficiency
        if top is not None:
            all_results = base_query.all()
            top_results: list[RecallByDefinitionSplitKind] = sorted(
                all_results, key=lambda r: r.recall_stats.mean if r.recall_stats else 0.0, reverse=True
            )[:top]
            sections_to_show.append((f"Top {top} by Recall", top_results))

        if bottom is not None:
            all_results = base_query.all()
            bottom_results: list[RecallByDefinitionSplitKind] = sorted(
                all_results, key=lambda r: r.recall_stats.mean if r.recall_stats else 0.0
            )[:bottom]
            sections_to_show.append((f"Bottom {bottom} by Recall", bottom_results))

        # Display each section
        for section_title, definition_results in sections_to_show:
            console.print(
                f"\n[bold]Aggregated Recall by Definition: {section_title}[/bold] ({len(definition_results)} results)\n"
            )

            columns: list[ColumnDef[Any, Any]] = [
                ColumnDef("Definition", lambda r: r.critic_image_digest, width=20),
                ColumnDef("Example Kind", lambda r: r.example_kind, width=15),
                *_DIMENSION_COLUMNS,
                *_OCCURRENCE_COLUMNS,
            ]

            table = build_table_from_schema(definition_results, columns)
            console.print(table)


@stats_app.command("example")
def cmd_stats_example(
    split: Split | None = None,
    critic_model: str | None = None,
    top: int | None = typer.Option(None, help="Show top N results by recall"),
    bottom: int | None = typer.Option(None, help="Show bottom N results by recall"),
) -> None:
    """Query aggregated recall metrics by example.

    Shows recall metrics aggregated by (split, snapshot_slug, example_kind, trigger_set_id, critic_model).
    Aggregates over all grader models (occurrence-based weighting).
    By default shows top 50 results. Use --top/--bottom to customize.
    """
    console = Console()

    # Default to showing top 50 if neither top nor bottom is specified
    if top is None and bottom is None:
        top = 50

    with get_session() as session:
        # Build base query with filters
        base_query = session.query(RecallByExample)
        if split:
            base_query = base_query.filter(RecallByExample.split == split)
        if critic_model:
            base_query = base_query.filter(RecallByExample.critic_model == critic_model)

        # Fetch top and/or bottom results
        sections_to_show: list[tuple[str, list[RecallByExample]]] = []

        # TODO: Move sorting to SQL side using (recall_stats).mean for efficiency
        if top is not None:
            all_results = base_query.all()
            top_results = sorted(
                all_results, key=lambda r: r.recall_stats.mean if r.recall_stats else 0.0, reverse=True
            )[:top]
            sections_to_show.append((f"Top {top} by Recall", top_results))

        if bottom is not None:
            all_results = base_query.all()
            bottom_results = sorted(all_results, key=lambda r: r.recall_stats.mean if r.recall_stats else 0.0)[:bottom]
            sections_to_show.append((f"Bottom {bottom} by Recall", bottom_results))

        # Display each section
        for section_title, example_results in sections_to_show:
            console.print(
                f"\n[bold]Aggregated Recall by Example: {section_title}[/bold] ({len(example_results)} results)\n"
            )

            columns: list[ColumnDef[Any, Any]] = [
                ColumnDef("Snapshot", lambda r: r.snapshot_slug, width=25),
                ColumnDef("Example", lambda r: f"{str(r.example_kind)[:4]}:{r.files_hash or '-'}", width=12),
                *_DIMENSION_COLUMNS,
                *_OCCURRENCE_COLUMNS,
            ]

            table = build_table_from_schema(example_results, columns)
            console.print(table)


@stats_app.command("occurrence")
def cmd_stats_occurrence(
    split: Split | None = None,
    critic_model: str | None = None,
    grader_model: str | None = None,
    top: int | None = typer.Option(None, help="Show top N results by mean credit"),
    bottom: int | None = typer.Option(None, help="Show bottom N results by mean credit"),
) -> None:
    """Query per-occurrence statistics.

    Shows statistics for individual occurrences: mean/stddev/min/max credit, catch rate.
    By default shows top 50 results. Use --top/--bottom to customize.
    """
    console = Console()

    # Default to showing top 50 if neither top nor bottom is specified
    if top is None and bottom is None:
        top = 50

    with get_session() as session:
        # Build base query with filters
        base_query = session.query(OccurrenceStatistics)
        if split:
            base_query = base_query.filter(OccurrenceStatistics.split == split)
        if critic_model:
            base_query = base_query.filter(OccurrenceStatistics.critic_model == critic_model)
        if grader_model:
            base_query = base_query.filter(OccurrenceStatistics.grader_model == grader_model)

        # Fetch top and/or bottom results
        sections_to_show: list[tuple[str, list[OccurrenceStatistics]]] = []

        # TODO: Move sorting to SQL side using (credit_stats).mean for efficiency
        if top is not None:
            all_results = base_query.all()
            top_results = sorted(
                all_results, key=lambda r: r.credit_stats.mean if r.credit_stats else 0.0, reverse=True
            )[:top]
            sections_to_show.append((f"Top {top} by Mean Credit", top_results))

        if bottom is not None:
            all_results = base_query.all()
            bottom_results = sorted(all_results, key=lambda r: r.credit_stats.mean if r.credit_stats else 0.0)[:bottom]
            sections_to_show.append((f"Bottom {bottom} by Mean Credit", bottom_results))

        # Display each section
        for section_title, occurrence_results in sections_to_show:
            console.print(
                f"\n[bold]Occurrence Statistics: {section_title}[/bold] ({len(occurrence_results)} results)\n"
            )

            columns: list[ColumnDef[Any, Any]] = [
                ColumnDef("TP ID", lambda r: r.tp_id[:20], width=20),
                ColumnDef("Occ ID", lambda r: r.occurrence_id[:15], width=15),
                ColumnDef("Split", lambda r: r.split, width=6),
                ColumnDef("Critic", lambda r: r.critic_model, fmt_model, width=12),
                ColumnDef("Grader", lambda r: r.grader_model, fmt_model, width=12),
                ColumnDef(
                    "Mean",
                    lambda r: r.credit_stats.mean if r.credit_stats else None,
                    lambda v: fmt_float(v, decimals=2),
                    justify="right",
                    width=6,
                ),
                ColumnDef(
                    "Min",
                    lambda r: r.credit_stats.min if r.credit_stats else None,
                    lambda v: fmt_float(v, decimals=2),
                    justify="right",
                    width=5,
                ),
                ColumnDef(
                    "Max",
                    lambda r: r.credit_stats.max if r.credit_stats else None,
                    lambda v: fmt_float(v, decimals=2),
                    justify="right",
                    width=5,
                ),
                ColumnDef(
                    "LCB95",
                    lambda r: r.credit_stats.lcb95 if r.credit_stats else None,
                    lambda v: fmt_float(v, decimals=2),
                    justify="right",
                    width=6,
                ),
                ColumnDef(
                    "UCB95",
                    lambda r: r.credit_stats.ucb95 if r.credit_stats else None,
                    lambda v: fmt_float(v, decimals=2),
                    justify="right",
                    width=6,
                ),
                ColumnDef("N", lambda r: r.credit_stats.n if r.credit_stats else 0, str, justify="right", width=4),
            ]

            table = build_table_from_schema(occurrence_results, columns)
            console.print(table)


@stats_app.callback(invoke_without_command=True)
def cmd_stats(ctx: typer.Context) -> None:
    """Display prompt statistics: count, runs per split, recall metrics.

    Run without subcommand to see overall statistics, or use subcommands for specific views:
    - prompt: View aggregated recall by prompt
    - example: View aggregated recall by example
    - occurrence: View per-occurrence statistics

    TODO: Add multi-level column headers (valid/train grouping) when Rich supports it.
    Currently Rich doesn't support column spanning (Issue #1529, #164), so we use
    prefixed column names. Workarounds: color-coded headers, visual separators, or
    wait for upstream support.
    """
    # Only run default stats if no subcommand was invoked
    if ctx.invoked_subcommand is not None:
        return

    console = Console()
    max_recalls_per_sample: dict[Split, list[float]] = defaultdict(list)
    tp_counts_per_sample: dict[Split, dict[ExampleSpec, int]] = defaultdict(dict)

    # Track run statuses by type
    status_counts: dict[str, defaultdict[AgentRunStatus, int]] = {
        "Critic": defaultdict(int),
        "Grader": defaultdict(int),
    }
    # Track example counts by (split, example_kind)
    example_counts: dict[tuple[Split, ExampleKind], int] = {}

    with get_session() as session:
        # Compute total available training examples per split using shared logic
        # IMPORTANT: Uses same logic as GEPA's dataset loading:
        # - TRAIN: all critic scopes (per-file + full-specimen for tighter feedback loops)
        # - VALID/TEST: only full-specimen scopes (terminal metric - comprehensive review)
        total_samples_by_split: dict[Split, int] = {
            split: count_available_examples_for_split(session, split)
            for split in [Split.TRAIN, Split.VALID, Split.TEST]
        }

        # Get counts for train and valid splits broken down by scope (single query)
        example_counts = count_available_examples_by_scope_all(session, [Split.TRAIN, Split.VALID])

        # No longer building prompt_stats_list here - using query builder instead

        # Compute best_count per split: how many samples each prompt is best on (or tied for best)
        # Query aggregated recall by example view (occurrence-weighted)
        # Key: ExampleSpec (frozen Pydantic union, hashable)
        sample_results_by_split: dict[Split, dict[ExampleSpec, dict[str, float]]] = {
            Split.TRAIN: defaultdict(dict),
            Split.VALID: defaultdict(dict),
            Split.TEST: defaultdict(dict),
        }

        for split in [Split.TRAIN, Split.VALID, Split.TEST]:
            # Query occurrence-weighted recall per (example, prompt) using helper
            results = query_recall_by_example(session, split=split)

            for recall_row in results:
                # Use ExampleSpec directly as key (frozen, hashable)
                sample_results_by_split[split][recall_row.example][recall_row.critic_image_digest] = recall_row.recall

            # Get TP counts per sample (constant per example, not per prompt/run)
            tp_count_results = (
                session.query(
                    OccurrenceCredit.snapshot_slug,
                    OccurrenceCredit.example_kind,
                    OccurrenceCredit.files_hash,
                    func.count(func.distinct(OccurrenceCredit.occurrence_id)).label("n_occurrences"),
                )
                .filter(OccurrenceCredit.split == split)
                .group_by(OccurrenceCredit.snapshot_slug, OccurrenceCredit.example_kind, OccurrenceCredit.files_hash)
                .all()
            )

            for result in tp_count_results:
                # Build ExampleSpec from query result
                if result.example_kind == ExampleKind.WHOLE_SNAPSHOT:
                    sample_key: ExampleSpec = WholeSnapshotExample(snapshot_slug=result.snapshot_slug)
                else:
                    sample_key = SingleFileSetExample(snapshot_slug=result.snapshot_slug, files_hash=result.files_hash)
                tp_counts_per_sample[split][sample_key] = result.n_occurrences

        # For each split and sample, find which prompt(s) achieved max recall
        prompt_best_counts: Counter[str] = Counter()
        for split in [Split.TRAIN, Split.VALID, Split.TEST]:
            for sample_recalls in sample_results_by_split[split].values():
                if not sample_recalls:
                    continue
                max_recall = max(sample_recalls.values())
                max_recalls_per_sample[split].append(max_recall)

                # Count best prompts for valid split only (for table display)
                if split == Split.VALID:
                    best_prompts = [sha for sha, recall in sample_recalls.items() if recall == max_recall]
                    prompt_best_counts.update(best_prompts)

        # Count run statuses using SQL aggregation
        # Filter to same scope as per-prompt stats: TRAIN + VALID (all examples)
        # Two-phase for unified AgentRun model

        # Critic status counts - pre-fetch snapshots to avoid N+1
        critic_runs = (
            session.query(AgentRun).filter(AgentRun.type_config["agent_type"].astext == AgentType.CRITIC).all()
        )
        snapshot_slugs = {
            cr.type_config.example.snapshot_slug for cr in critic_runs if isinstance(cr.type_config, CriticTypeConfig)
        }
        snapshots = session.query(Snapshot).filter(Snapshot.slug.in_(snapshot_slugs)).all() if snapshot_slugs else []
        snapshot_by_slug = {s.slug: s for s in snapshots}

        critic_status_counts: Counter[AgentRunStatus] = Counter()
        for cr in critic_runs:
            if not isinstance(cr.type_config, CriticTypeConfig):
                continue
            snapshot_slug = cr.type_config.example.snapshot_slug
            snapshot = snapshot_by_slug.get(snapshot_slug)
            if snapshot and snapshot.split in (Split.TRAIN, Split.VALID):
                critic_status_counts[cr.status] += 1

        for status, count in critic_status_counts.items():
            status_counts["Critic"][status] = count

        # Grader status counts - pre-fetch graded runs and snapshots to avoid N+1
        grader_runs = (
            session.query(AgentRun).filter(AgentRun.type_config["agent_type"].astext == AgentType.GRADER).all()
        )
        graded_run_ids = {gr.grader_config().graded_agent_run_id for gr in grader_runs}
        graded_runs = (
            session.query(AgentRun).filter(AgentRun.agent_run_id.in_(graded_run_ids)).all() if graded_run_ids else []
        )
        graded_run_by_id = {r.agent_run_id: r for r in graded_runs}

        # Already have snapshots from critic query above, but need to expand for graded runs
        additional_snapshot_slugs = {
            graded_run.critic_config().example.snapshot_slug
            for graded_run in graded_runs
            if isinstance(graded_run.type_config, CriticTypeConfig)
        } - snapshot_slugs
        if additional_snapshot_slugs:
            additional_snapshots = session.query(Snapshot).filter(Snapshot.slug.in_(additional_snapshot_slugs)).all()
            snapshot_by_slug.update({s.slug: s for s in additional_snapshots})

        grader_status_counts: Counter[AgentRunStatus] = Counter()
        for gr in grader_runs:
            grader_config = gr.grader_config()
            graded_run = graded_run_by_id.get(grader_config.graded_agent_run_id)
            if graded_run and isinstance(graded_run.type_config, CriticTypeConfig):
                snapshot_slug = graded_run.critic_config().example.snapshot_slug
                snapshot = snapshot_by_slug.get(snapshot_slug)
                if snapshot and snapshot.split in (Split.TRAIN, Split.VALID):
                    grader_status_counts[gr.status] += 1

        for status, count in grader_status_counts.items():
            status_counts["Grader"][status] = count

    # Query aggregated view and group by definition
    agg_results = (
        session.query(RecallByDefinitionSplitKind)
        .filter(RecallByDefinitionSplitKind.split.in_([Split.TRAIN, Split.VALID]))
        .all()
    )

    # Group by definition_id and build stats dict
    definition_stats: dict[str, dict[tuple[Split, ExampleKind], RecallByDefinitionSplitKind]] = {}
    for row in agg_results:
        if row.critic_image_digest not in definition_stats:
            definition_stats[row.critic_image_digest] = {}
        definition_stats[row.critic_image_digest][(row.split, row.example_kind)] = row

    # Get definition metadata (created_at) from agent_definitions table
    definition_metadata: dict[str, AgentDefinition] = {}
    if definition_stats:
        definitions = session.query(AgentDefinition).filter(AgentDefinition.digest.in_(definition_stats.keys())).all()
        for d in definitions:
            definition_metadata[d.digest] = d

    # Sort by created_at DESC
    sorted_definition_ids = sorted(
        definition_stats.keys(),
        key=lambda d: definition_metadata[d].created_at if d in definition_metadata else datetime.min,
        reverse=True,
    )[:100]  # Limit to 100

    # Display summary
    console.print(f"\n[bold]Agent Definition Statistics[/bold] ({len(sorted_definition_ids)} definitions)\n")

    # Create new table with requested columns
    table = Table(show_header=True, header_style="bold cyan", box=box.HORIZONTALS, show_edge=False, padding=(0, 0))
    table.add_column("Definition", style="dim", width=12)
    table.add_column("Age", justify="right", width=4)

    # Define canonical split/scope ordering with colors
    split_scope_config = [
        ((Split.VALID, ExampleKind.WHOLE_SNAPSHOT), "cyan"),
        ((Split.VALID, ExampleKind.FILE_SET), "blue"),
        ((Split.TRAIN, ExampleKind.WHOLE_SNAPSHOT), "yellow"),
        ((Split.TRAIN, ExampleKind.FILE_SET), "magenta"),
    ]

    # Helper to compute label from split and scope kind
    def make_label(split: Split, scope_kind: ExampleKind) -> str:
        split_abbrev = {Split.VALID: "Val", Split.TRAIN: "Tr", Split.TEST: "Test"}[split]
        scope_abbrev = {ExampleKind.WHOLE_SNAPSHOT: "W", ExampleKind.FILE_SET: "P"}[scope_kind]
        return f"{split_abbrev}-{scope_abbrev}"

    # Add split columns using canonical order
    for key, color in split_scope_config:
        label = make_label(key[0], key[1])
        _add_split_columns(table, label, color, example_counts[key])

    def format_view_stats(stats: RecallByDefinitionSplitKind | None, fully_computed: bool = False) -> tuple[str, ...]:
        """Format view stats for the multi-column-group table as (recall, lcb, n, zero, completed, stuck, context, failure).

        This is a local helper for the main stats table which uses a different layout
        than the row-per-item tables that use _OCCURRENCE_COLUMNS / _STATUS_COLUMNS.
        """
        if stats is None:
            return ("—", "—", "—", "—", "—", "—", "—", "—")

        # Use fmt_pct for percentage formatting (handles None -> "—")
        recall_val = stats.recall_stats.mean if stats.recall_stats else None
        recall_str = fmt_pct(recall_val)
        if fully_computed and recall_val is not None:
            recall_str = f"[green]{recall_str}[/green]"

        lcb_str = fmt_pct(stats.recall_stats.lcb95 if stats.recall_stats else None)

        return (
            recall_str,
            lcb_str,
            str(stats.n_examples or 0),
            str(stats.zero_count or 0),
            str(stats.status_counts.get(AgentRunStatus.COMPLETED, 0)),
            str(stats.status_counts.get(AgentRunStatus.MAX_TURNS_EXCEEDED, 0)),
            str(stats.status_counts.get(AgentRunStatus.CONTEXT_LENGTH_EXCEEDED, 0)),
            str(stats.status_counts.get(AgentRunStatus.REPORTED_FAILURE, 0)),
        )

    for definition_id in sorted_definition_ids:
        stats_dict = definition_stats[definition_id]
        meta = definition_metadata.get(definition_id)
        age_str = format_age(meta.created_at) if meta else "—"

        # Format stats for all split/scope combinations using canonical order
        formatted_stats = []
        for key, _color in split_scope_config:
            stats = stats_dict.get(key)
            # Check if fully computed
            n_examples = stats.n_examples if stats else 0
            fully_computed = n_examples == example_counts[key]
            formatted_stats.append(format_view_stats(stats, fully_computed=fully_computed))

        table.add_row(
            definition_id,
            age_str,
            # Unpack all formatted stats in canonical order
            *[field for stats in formatted_stats for field in stats],
        )

    console.print(table)

    # Display legend
    console.print(STATS_TABLE_LEGEND)

    console.print("[bold]Summary:[/bold]")
    console.print(f"  Total definitions: {len(sorted_definition_ids)}")
    console.print("\n  Available examples per split:")
    for split in [Split.VALID, Split.TRAIN]:
        console.print(
            f"    {split.value.capitalize()}: {total_samples_by_split[split]} total "
            f"(whole: {example_counts[(split, ExampleKind.WHOLE_SNAPSHOT)]}, "
            f"partial: {example_counts[(split, ExampleKind.FILE_SET)]})"
        )
    console.print(f"    Test: {total_samples_by_split[Split.TEST]}")

    # Find best definition by valid whole-snapshot recall (terminal metric)
    valid_whole_definitions = []
    for definition_id in sorted_definition_ids:
        stats = definition_stats[definition_id].get((Split.VALID, ExampleKind.WHOLE_SNAPSHOT))
        if stats is not None and stats.recall_stats:
            valid_whole_definitions.append((definition_id, stats))

    if valid_whole_definitions:
        # TODO: Move sorting to SQL side using (recall_stats).mean for efficiency
        best_def_id, best_stats = max(
            valid_whole_definitions, key=lambda x: x[1].recall_stats.mean if x[1].recall_stats else 0.0
        )
        n_completed = best_stats.status_counts.get(AgentRunStatus.COMPLETED, 0)
        recall_mean = best_stats.recall_stats.mean if best_stats.recall_stats else 0.0
        console.print(
            f"\n[bold green]Best definition (valid whole-snapshot):[/bold green] "
            f"{best_def_id} with {recall_mean:.1%} recall "
            f"({n_completed}/{best_stats.n_examples} runs)"
        )

    # Display run status statistics as table
    console.print("\n[bold cyan]Run Status Statistics[/bold cyan]")

    # Collect all unique statuses across all run types
    all_statuses = sorted(
        set().union(*(counts.keys() for counts in status_counts.values())),
        key=lambda x: (x is None, x),  # None (no tag) sorts last
    )

    # Build table with dynamic columns
    status_table = Table(show_header=True, box=box.SIMPLE)
    status_table.add_column("Type", style="bold")
    status_table.add_column("Total", justify="right")

    for status in all_statuses:
        label = status.value
        status_table.add_column(label, justify="right")

    # Add rows for each run type
    for run_type, counts in status_counts.items():
        total = sum(counts.values())
        if total > 0:
            row_data = [run_type, str(total)]
            for status in all_statuses:
                count = counts[status]
                pct = count / total
                row_data.append(f"{count} ({pct:.1%})")
            status_table.add_row(*row_data)

    console.print(status_table)

    # Display distributions for each split
    for split in [Split.TRAIN, Split.VALID, Split.TEST]:
        split_name = split.value.capitalize()

        # Display distribution of max recall scores
        if max_recalls_per_sample[split]:
            recall_buckets = _generate_buckets(max_recalls_per_sample[split], num_buckets=10)
            _display_distribution(
                console,
                max_recalls_per_sample[split],
                f"Max Recall Distribution ({split_name} Examples)",
                recall_buckets,
                value_format="{:.1f}%",
            )

        # Display distribution of TP counts
        if tp_counts_per_sample[split]:
            tp_counts = list(tp_counts_per_sample[split].values())
            tp_buckets = _generate_buckets(tp_counts, num_buckets=10)
            _display_distribution(
                console,
                tp_counts,
                f"True Positive Count Distribution ({split_name} Examples)",
                tp_buckets,
                value_format="{:.0f}",
            )

        # Display split analysis (zero-recall and best coverage)
        # Show all prompts for training split, top 15 for others
        show_all = split == Split.TRAIN
        split_total = total_samples_by_split[split]
        _display_split_analysis(
            console,
            split_name,
            sample_results_by_split[split],
            tp_counts_per_sample[split],
            total_available=split_total,
            show_all_prompts=show_all,
        )

    # Display tool call count distributions for successful runs
    with get_session() as session:
        # Query tool call counts for successful critic runs (events linked directly to agent_run_id)
        critic_run_ids = [
            cr.agent_run_id
            for cr in session.query(AgentRun)
            .filter(
                AgentRun.type_config["agent_type"].astext == AgentType.CRITIC,
                AgentRun.status == AgentRunStatus.COMPLETED,
            )
            .all()
        ]

        if critic_run_ids:
            critic_tool_calls = (
                session.query(Event.agent_run_id, func.count(Event.id).label("tool_call_count"))
                .where(Event.event_type == "tool_call")
                .where(Event.agent_run_id.in_(critic_run_ids))
                .group_by(Event.agent_run_id)
                .all()
            )
        else:
            critic_tool_calls = []

        # Query tool call counts for successful grader runs (events linked directly to agent_run_id)
        grader_run_ids = [
            gr.agent_run_id
            for gr in session.query(AgentRun)
            .filter(
                AgentRun.type_config["agent_type"].astext == AgentType.GRADER,
                AgentRun.status == AgentRunStatus.COMPLETED,
            )
            .all()
        ]

        if grader_run_ids:
            grader_tool_calls = (
                session.query(Event.agent_run_id, func.count(Event.id).label("tool_call_count"))
                .where(Event.event_type == "tool_call")
                .where(Event.agent_run_id.in_(grader_run_ids))
                .group_by(Event.agent_run_id)
                .all()
            )
        else:
            grader_tool_calls = []

    # Display critic tool call distribution
    if critic_tool_calls:
        critic_counts = [count for _, count in critic_tool_calls]
        critic_buckets = _generate_buckets(critic_counts, num_buckets=10, equal_width=True)
        _display_distribution(
            console, critic_counts, "Tool Calls per Successful Critic Run", critic_buckets, value_format="{:.0f}"
        )

    # Display grader tool call distribution
    if grader_tool_calls:
        grader_counts = [count for _, count in grader_tool_calls]
        grader_buckets = _generate_buckets(grader_counts, num_buckets=10, equal_width=True)
        _display_distribution(
            console, grader_counts, "Tool Calls per Successful Grader Run", grader_buckets, value_format="{:.0f}"
        )

    # Check for stale grader runs
    console.print("\n[bold cyan]Grader Run Staleness Check[/bold cyan]")
    console.print("=" * 60)

    stale_run_ids, by_snapshot = identify_stale_runs()
    stale_runs = len(stale_run_ids)
    total_runs = sum(stats["total"] for stats in by_snapshot.values())

    if total_runs == 0:
        console.print("No grader runs found in database")
    else:
        stale_pct = (stale_runs / total_runs * 100) if total_runs > 0 else 0
        console.print(f"\nTotal grader runs: {total_runs}")
        console.print(f"Stale runs: {stale_runs} ({stale_pct:.1f}%)")
        console.print(f"Up-to-date runs: {total_runs - stale_runs} ({100 - stale_pct:.1f}%)")

        if stale_runs > 0:
            console.print("\n[bold]Stale runs by snapshot:[/bold]")

            # Filter to snapshots with stale runs and prepare data
            stale_snapshot_data = [
                (slug, by_snapshot[slug]) for slug in sorted(by_snapshot.keys()) if by_snapshot[slug]["stale"] > 0
            ]

            columns: list[ColumnDef[Any, Any]] = [
                ColumnDef("Snapshot", lambda r: str(r[0]), style="cyan"),
                ColumnDef("Total", lambda r: r[1]["total"], str, justify="right"),
                ColumnDef("Stale", lambda r: r[1]["stale"], str, justify="right"),
                ColumnDef(
                    "Stale %",
                    lambda r: (r[1]["stale"] / r[1]["total"] * 100) if r[1]["total"] > 0 else 0,
                    lambda v: f"{v:.1f}%",
                    justify="right",
                ),
            ]

            table = build_table_from_schema(stale_snapshot_data, columns)
            console.print(table)
