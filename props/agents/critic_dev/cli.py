"""Critic development CLI for optimizer and improvement agents.

Commands for running critic/grader evaluations, viewing metrics, and analysis.
Used by critic-dev agents running inside containers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any
from uuid import UUID

import typer
from rich.console import Console
from sqlalchemy import text

from props.agents.critic_dev.cli_helpers import show_execution_traces, show_grading_summary, show_run_status
from props.agents.runtime import get_current_agent_run, get_current_agent_run_id
from props.cli.cmd_stats import cmd_stats_critic_leaderboard, cmd_stats_example, fmt_float, fmt_model, fmt_pct
from props.core.agent_types import AgentType
from props.core.display import ColumnDef, build_table_from_schema
from props.core.splits import Split
from props.db.database import Database

HELP_TEXT = """Critic development commands for iterating on agent definitions.

Common workflows:

  Analyze runs spawned by this agent:
    props critic-dev run-status
    props critic-dev traces --limit 10
    props critic-dev grading-summary "critic-or-grader-run-uuid"

  View metrics (definitions and examples):
    props critic-dev leaderboard
    props critic-dev valid-leaderboard  # whole-repo mode only
    props critic-dev hard-examples --limit 10
"""

app = typer.Typer(name="critic-dev", help=HELP_TEXT, add_completion=False)


@app.callback()
def _critic_dev_callback(ctx: typer.Context) -> None:
    """Initialize Database for critic-dev commands."""
    if ctx.obj is None:
        ctx.obj = Database.from_env()


@app.command("run-status")
def run_status_cmd(ctx: typer.Context) -> None:
    """Show run status statistics for critic and grader runs spawned by this agent."""
    db: Database = ctx.obj
    with db.session() as session:
        parent_id = get_current_agent_run_id(session)
    show_run_status(db, parent_agent_run_id=parent_id)


@app.command("traces")
def traces_cmd(
    ctx: typer.Context, limit: Annotated[int, typer.Option("--limit", "-n", help="Number of recent runs to show")] = 5
) -> None:
    """Show execution traces for recent critic runs spawned by this agent."""
    db: Database = ctx.obj
    with db.session() as session:
        parent_id = get_current_agent_run_id(session)
    show_execution_traces(db, limit=limit, parent_agent_run_id=parent_id)


@app.command("grading-summary")
def grading_summary_cmd(
    ctx: typer.Context, run_id: Annotated[str, typer.Argument(help="UUID of a critic or grader run")]
) -> None:
    """Show grading decision summary for a critic or grader run."""
    db: Database = ctx.obj
    show_grading_summary(db, agent_run_id=UUID(run_id))


@app.command("leaderboard")
def leaderboard_cmd(
    ctx: typer.Context, limit: Annotated[int, typer.Option("--limit", "-n", help="Number of definitions to show")] = 20
) -> None:
    """Show top definitions by recall on accessible data."""
    db: Database = ctx.obj
    with db.session() as session:
        agent_run = get_current_agent_run(session)
        split_filter = Split.TRAIN if agent_run.type_config.agent_type == AgentType.CRITIC_DEV_OPTIMIZE else None
    cmd_stats_critic_leaderboard(ctx, split=split_filter, example_kind=None, top=limit, bottom=None)


@app.command("hard-examples")
def hard_examples_cmd(
    ctx: typer.Context, limit: Annotated[int, typer.Option("--limit", "-n", help="Number of examples to show")] = 20
) -> None:
    """Show examples with lowest recall (hardest to solve) on accessible data."""
    db: Database = ctx.obj
    with db.session() as session:
        agent_run = get_current_agent_run(session)
        split_filter = Split.TRAIN if agent_run.type_config.agent_type == AgentType.CRITIC_DEV_OPTIMIZE else None
    cmd_stats_example(ctx, split=split_filter, top=None, bottom=limit)


@app.command("valid-leaderboard")
def valid_leaderboard_cmd(
    ctx: typer.Context, limit: Annotated[int, typer.Option("--limit", "-n", help="Number of definitions to show")] = 20
) -> None:
    """Show top definitions by recall on validation split (whole-snapshot only).

    Uses SECURITY DEFINER function to access black-box validation metrics.
    Shows occurrence-weighted recall (total_credit / n_occurrences).
    """
    console = Console()

    @dataclass
    class ValidationLeaderboardRow:
        """Row from validation leaderboard query."""

        critic_image_digest: str
        critic_model: str
        n_runs: int
        sum_credit: float | None
        sum_occurrences: int | None
        mean_recall: float | None
        stddev_recall: float | None

    db: Database = ctx.obj
    with db.session() as session:
        raw_results = session.execute(
            text("""
                SELECT
                    critic_image_digest,
                    critic_model,
                    COUNT(*) as n_runs,
                    SUM(total_credit) as sum_credit,
                    SUM(n_occurrences) as sum_occurrences,
                    AVG(total_credit / NULLIF(n_occurrences, 0)) as mean_recall,
                    STDDEV_SAMP(total_credit / NULLIF(n_occurrences, 0)) as stddev_recall
                FROM get_validation_full_snapshot_aggregates()
                GROUP BY critic_image_digest, critic_model
                ORDER BY mean_recall DESC
                LIMIT :limit
            """),
            {"limit": limit},
        ).fetchall()

        if not raw_results:
            console.print("[yellow]No validation results found.[/yellow]")
            return

        results = [
            ValidationLeaderboardRow(
                critic_image_digest=row[0],
                critic_model=row[1] or "",
                n_runs=row[2] or 0,
                sum_credit=row[3],
                sum_occurrences=int(row[4]) if row[4] is not None else None,
                mean_recall=row[5],
                stddev_recall=row[6],
            )
            for row in raw_results
        ]

        columns: list[ColumnDef[ValidationLeaderboardRow, Any]] = [
            ColumnDef("Definition", lambda r: r.critic_image_digest[:20], width=20),
            ColumnDef("Model", lambda r: r.critic_model, fmt_model, width=12),
            ColumnDef("Runs", lambda r: r.n_runs, str, justify="right", width=5),
            ColumnDef("Credit", lambda r: r.sum_credit, lambda v: fmt_float(v, decimals=1), justify="right", width=7),
            ColumnDef(
                "Occs",
                lambda r: r.sum_occurrences,
                lambda v: str(v) if v is not None else "-",
                justify="right",
                width=6,
            ),
            ColumnDef("Recall", lambda r: r.mean_recall, fmt_pct, justify="right", width=7),
            ColumnDef("s", lambda r: r.stddev_recall, lambda v: fmt_float(v, decimals=3), justify="right", width=6),
        ]

        console.print(f"\n[bold]Top {limit} Definitions by Validation Recall (Occurrence-Weighted)[/bold]\n")
        table = build_table_from_schema(results, columns)
        console.print(table)
