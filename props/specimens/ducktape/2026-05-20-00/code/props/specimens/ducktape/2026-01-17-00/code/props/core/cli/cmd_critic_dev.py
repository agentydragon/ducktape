"""Critic development CLI for optimizer and improvement agents.

Commands for running critic/grader evaluations, viewing metrics, and analysis.
Used by prompt optimizer and improvement agents running inside containers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any
from uuid import UUID

import typer
from rich.console import Console
from sqlalchemy import text

from agent_pkg.runtime.mcp import mcp_client_from_env
from agent_pkg.runtime.output import render_agent_prompt
from cli_util.decorators import async_run
from props.core.agent_helpers import get_current_agent_run, get_current_agent_run_id
from props.core.agent_types import AgentType
from props.core.cli.cmd_critic_dev_helpers import show_execution_traces, show_grading_summary, show_run_status
from props.core.cli.cmd_stats import cmd_stats_critic_leaderboard, cmd_stats_example, fmt_float, fmt_model, fmt_pct
from props.core.db.session import get_session
from props.core.display import ColumnDef, build_table_from_schema
from props.core.models.examples import ExampleSpec, SingleFileSetExample, WholeSnapshotExample
from props.core.prompt_optimize.prompt_optimizer import ReportFailureInput, RunCriticInput, RunGraderInput
from props.core.splits import Split

HELP_TEXT = """Critic development commands for iterating on agent definitions.

Common workflows:

  Run evaluation pipeline:
    props critic-dev run-critic "my-def-id" "snapshot-slug" "scope-hash"
    props critic-dev run-grader "critic-run-uuid"

  Analyze runs spawned by this agent:
    props critic-dev run-status
    props critic-dev traces --limit 10
    props critic-dev grading-summary "critic-or-grader-run-uuid"

  View metrics (definitions and examples):
    props critic-dev leaderboard
    props critic-dev valid-leaderboard  # whole-repo mode only
    props critic-dev hard-examples --limit 10

  Report failure and abort:
    props critic-dev report-failure "Error message"
"""

app = typer.Typer(name="critic-dev", help=HELP_TEXT, add_completion=False)


@app.command("run-critic")
@async_run
async def run_critic_cmd(
    image_ref: Annotated[str, typer.Argument(help="Agent image reference (tag or digest, or 'critic' for baseline)")],
    snapshot_slug: Annotated[str, typer.Argument(help="Snapshot identifier (e.g., 'test-fixtures/train1')")],
    files_hash: Annotated[
        str | None, typer.Argument(help="Files hash for file_set example, or omit/empty for whole_snapshot")
    ] = None,
    max_turns: Annotated[int, typer.Option("--max-turns", "-t", help="Maximum agent turns before timeout")] = 200,
) -> None:
    """Run critic on an example using an agent image.

    Returns the critic_run_id which can be used with run-grader.

    Examples:
        # Whole snapshot review
        props critic-dev run-critic critic "test-fixtures/train1"

        # File set review (specific files)
        props critic-dev run-critic critic "ducktape/2025-11-26-00" "abc123..."
    """
    # Construct example spec based on whether files_hash is provided
    example: ExampleSpec
    if files_hash:
        example = SingleFileSetExample(snapshot_slug=snapshot_slug, files_hash=files_hash)
    else:
        example = WholeSnapshotExample(snapshot_slug=snapshot_slug)

    payload = RunCriticInput(definition_id=image_ref, example=example, max_turns=max_turns)

    async with mcp_client_from_env() as (client, _init_result):
        result = await client.call_tool("run_critic", payload.model_dump())
        typer.echo(f"Critic run ID: {result}")


@app.command("run-grader")
@async_run
async def run_grader_cmd(
    critic_run_id: Annotated[str, typer.Argument(help="UUID of the critic run to grade")],
    max_turns: Annotated[int, typer.Option("--max-turns", "-t", help="Maximum agent turns before timeout")] = 200,
) -> None:
    """Grade a critique and compute recall metrics.

    Takes a critic_run_id and runs the grader to match reported issues
    against ground truth. Returns the grader_run_id.

    Examples:
        props critic-dev run-grader "12345678-1234-1234-1234-123456789abc"
        props critic-dev run-grader "12345678-..." --max-turns 100
    """
    payload = RunGraderInput(critic_run_id=UUID(critic_run_id), max_turns=max_turns)

    async with mcp_client_from_env() as (client, _init_result):
        result = await client.call_tool("run_grader", payload.model_dump(mode="json"))
        typer.echo(f"Grader run result: {result}")


@app.command("report-failure")
@async_run
async def report_failure_cmd(
    message: Annotated[str, typer.Argument(help="Error message explaining why the agent could not complete")],
) -> None:
    """Report that the agent could not complete and should abort.

    Use this when the optimization or improvement run should be aborted
    (e.g., critical errors, no viable path forward, budget exceeded).

    Examples:
        props critic-dev report-failure "Budget exceeded after 50 evaluations"
        props critic-dev report-failure "No examples available for this split"
    """
    payload = ReportFailureInput(message=message)

    async with mcp_client_from_env() as (client, _init_result):
        result = await client.call_tool("report_failure", payload.model_dump())
        typer.echo(result)


@app.command("run-status")
def run_status_cmd() -> None:
    """Show run status statistics for critic and grader runs spawned by this agent.

    Displays counts of runs by status (completed, max_turns_exceeded, etc.)
    and identifies definitions with high failure rates.
    """
    with get_session() as session:
        parent_id = get_current_agent_run_id(session)
    show_run_status(parent_agent_run_id=parent_id)


@app.command("traces")
def traces_cmd(limit: Annotated[int, typer.Option("--limit", "-n", help="Number of recent runs to show")] = 5) -> None:
    """Show execution traces for recent critic runs spawned by this agent.

    Lists recent critic runs with tool counts and shows the full trace
    for the most recent run. Useful for understanding agent behavior patterns.
    """
    with get_session() as session:
        parent_id = get_current_agent_run_id(session)
    show_execution_traces(limit=limit, parent_agent_run_id=parent_id)


@app.command("grading-summary")
def grading_summary_cmd(run_id: Annotated[str, typer.Argument(help="UUID of a critic or grader run")]) -> None:
    """Show grading decision summary for a critic or grader run.

    Accepts either a critic run ID (finds associated grader) or grader run ID directly.
    Displays credit breakdown, TP/occurrence counts, and missed issues.
    """
    show_grading_summary(agent_run_id=UUID(run_id))


@app.command("leaderboard")
def leaderboard_cmd(
    limit: Annotated[int, typer.Option("--limit", "-n", help="Number of definitions to show")] = 20,
) -> None:
    """Show top definitions by recall on accessible data.

    For prompt_optimizer: Shows TRAIN split metrics (VALID requires SECURITY DEFINER function).
    For improvement: Shows metrics for allowed_examples (any split).
    """
    with get_session() as session:
        agent_run = get_current_agent_run(session)
        split_filter = Split.TRAIN if agent_run.type_config.agent_type == AgentType.PROMPT_OPTIMIZER else None
    cmd_stats_critic_leaderboard(split=split_filter, example_kind=None, top=limit, bottom=None)


@app.command("hard-examples")
def hard_examples_cmd(
    limit: Annotated[int, typer.Option("--limit", "-n", help="Number of examples to show")] = 20,
) -> None:
    """Show examples with lowest recall (hardest to solve) on accessible data.

    For prompt_optimizer: Shows TRAIN split examples.
    For improvement: Shows allowed_examples (any split).
    """
    with get_session() as session:
        agent_run = get_current_agent_run(session)
        split_filter = Split.TRAIN if agent_run.type_config.agent_type == AgentType.PROMPT_OPTIMIZER else None
    cmd_stats_example(split=split_filter, top=None, bottom=limit)


@app.command("valid-leaderboard")
def valid_leaderboard_cmd(
    limit: Annotated[int, typer.Option("--limit", "-n", help="Number of definitions to show")] = 20,
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

    with get_session() as session:
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


@app.command("init")
def init_cmd() -> None:
    """Run bootstrap for prompt_optimizer/improvement agents (called by /init script).

    Renders the base critic scaffold which includes:
    - Agent-specific advice from /agent.md
    - Shared documentation and CLI help
    """
    with get_session() as session:
        agent_run = get_current_agent_run(session)
        config = agent_run.type_config

    render_agent_prompt("props/docs/agents/critic_dev.md.j2", helpers={"config": config})
