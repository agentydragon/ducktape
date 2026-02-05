"""Typer-based CLI entry for props.

Incremental migration target: we will gradually move subcommands here.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

import aiodocker
import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.traceback import install as rich_traceback_install
from typer_di import TyperDI

from cli_util.decorators import async_run
from cli_util.logging import LogLevel, make_logging_callback
from props.cli import common_options as opt
from props.cli.cmd_db import db_app
from props.cli.cmd_gt import gt_app
from props.cli.cmd_snapshot import snapshot_app
from props.cli.cmd_stats import stats_app
from props.cli.shared import make_example_from_files
from props.config import load_config_from_env
from props.core.agent_helpers import get_current_agent_run
from props.core.agent_types import AgentType
from props.core.display import fmt_pct, short_sha
from props.core.ids import DefinitionId, SnapshotSlug
from props.core.models.examples import ExampleKind, ExampleSpec, SingleFileSetExample, WholeSnapshotExample
from props.core.oci_utils import get_registry_proxy_config
from props.core.splits import Split
from props.critic_dev.improve.main import TerminationSuccess
from props.critic_dev.shared import TargetMetric
from props.db.database import Database
from props.db.models import AgentRun, AgentRunStatus, RecallByDefinitionSplitKind, ReportedIssue, Snapshot
from props.db.query_builders import query_recall_by_example

# cmd_gepa imported lazily below (gepa is optional)
from props.orchestration.agent_registry import (
    AgentRegistry,
    ImprovementResult,
    OutcomeExhausted,
    OutcomeUnexpectedTermination,
)

logger = logging.getLogger(__name__)

app = TyperDI(help="props — properties tooling", add_completion=False)

# Subcommand groups
app.add_typer(db_app, name="db")
app.add_typer(gt_app, name="gt")
app.add_typer(snapshot_app, name="snapshot")

# Configure logging via shared callback (default: WARNING level for props)
# Then add database initialization on top
_logging_callback = make_logging_callback(default_level=LogLevel.WARNING)


@app.callback()
def _init_logging_and_db(
    ctx: typer.Context,
    log_output: Annotated[
        str,
        typer.Option(
            "--log-output",
            envvar="ADGN_LOG_OUTPUT",
            help="Where to send logs: 'stderr', 'stdout', 'none', or a file path",
        ),
    ] = "stderr",
    log_level: Annotated[str, typer.Option("--log-level", envvar="ADGN_LOG_LEVEL", help="Log level")] = "WARNING",
) -> None:
    """Global callback to configure logging and initialize database for all subcommands."""
    # First, configure logging via the shared callback
    _logging_callback(log_output=log_output, log_level=log_level)

    # Suppress verbose OpenAI HTTP request/response logging (too noisy at DEBUG level)
    logging.getLogger("openai.http").setLevel(logging.WARNING)
    logging.getLogger("openai._base_client").setLevel(logging.WARNING)

    # Configure Rich traceback for CLI errors (increased detail for debugging)
    rich_traceback_install(show_locals=True, max_frames=50, extra_lines=2, width=120)

    # Create Database once at CLI entry. Stored on typer context for explicit DI.
    db = Database.from_env()
    ctx.obj = db


@app.command("run-info")
def run_info_cmd(ctx: typer.Context) -> None:
    """Show current agent run information (works for any agent type).

    Displays:
    - agent_run_id: The unique identifier for this run
    - agent_type: The type of agent (critic, grader, etc.)
    - type_config: The full configuration for this agent run
    """

    db: Database = ctx.obj
    with db.session() as session:
        agent_run = get_current_agent_run(session)
        typer.echo("Agent Run Info:")
        typer.echo(f"  agent_run_id: {agent_run.agent_run_id}")
        typer.echo(f"  image_digest: {agent_run.image_digest}")
        typer.echo(f"  model: {agent_run.model}")
        typer.echo(f"  status: {agent_run.status.value}")
        if agent_run.parent_agent_run_id:
            typer.echo(f"  parent_agent_run_id: {agent_run.parent_agent_run_id}")
        typer.echo()
        typer.echo("Type Config:")
        typer.echo(agent_run.type_config.model_dump_json(indent=2))


@app.command("prompt-optimize")
@async_run
async def prompt_optimize(
    ctx: typer.Context,
    target_metric: Annotated[
        TargetMetric,
        typer.Option(
            help="Terminal metric mode (REQUIRED): 'whole-repo' (black-box validation, only full-snapshot) or 'targeted' (allows per-file validation examples)"
        ),
    ],
    budget: float = typer.Option(50.0, "--budget", help="$ budget for optimization"),
    optimizer_model: str = opt.OPT_OPTIMIZER_MODEL,
    critic_model: str = opt.OPT_CRITIC_MODEL,
    timeout_seconds: int = opt.OPT_TIMEOUT_SECONDS,
) -> None:
    """Run a Prompt Engineering agent to optimize a critic system prompt using prompt_eval MCP with $ budget."""
    db: Database = ctx.obj
    config = load_config_from_env()
    docker_client = aiodocker.Docker()
    registry = AgentRegistry(
        docker_client,
        db=db,
        db_config=db.config,
        agent_base_env=config.agent_env,
        registry_config=get_registry_proxy_config(),
    )
    try:
        run_id = await registry.run_prompt_optimizer(
            budget=budget,
            optimizer_model=optimizer_model,
            critic_model=critic_model,
            target_metric=target_metric,
            timeout_seconds=timeout_seconds,
        )
        typer.echo(f"Prompt optimizer run ID: {run_id}")
    finally:
        await registry.close()


@app.command("prompt-improve")
@async_run
async def prompt_improve_cmd(
    ctx: typer.Context,
    n_examples: int = typer.Option(10, "--n-examples", "-n", help="Number of training examples to analyze"),
    token_budget: int = typer.Option(200_000, "--token-budget", help="Maximum token budget"),
    improvement_model: str = opt.OPT_OPTIMIZER_MODEL,
    critic_model: str = opt.OPT_CRITIC_MODEL,
    out_dir: Path | None = opt.OPT_OUT_DIR,
    timeout_seconds: int = opt.OPT_TIMEOUT_SECONDS,
) -> None:
    """Run prompt improvement agent on training examples.

    Selects N Pareto-optimal training examples and runs improvement agent with
    token budget enforcement. The agent analyzes failure patterns and proposes
    an improved prompt.

    Example:
        props prompt-improve
        props prompt-improve -n 20 -t 100000
    """
    console = Console()
    console.print("\n[bold cyan]Prompt Improvement Agent[/bold cyan]\n")

    # Helper function for Pareto selection
    def select_pareto_examples(session, agent_definition_id_param: str, limit: int) -> list[ExampleSpec]:
        """Select Pareto-optimal training examples for an agent definition."""
        # Query occurrence-weighted recall per example using helper
        results = query_recall_by_example(session, split=Split.TRAIN, critic_image_digest=agent_definition_id_param)

        if not results:
            raise ValueError(f"No grader runs found for definition {short_sha(agent_definition_id_param)}")

        # Build example scores dict (RecallByExampleRow already has ExampleSpec)
        example_scores: dict[ExampleSpec, float] = {row.example: row.recall for row in results}

        # Sort by recall descending and take top N
        sorted_examples = sorted(example_scores.items(), key=lambda x: x[1], reverse=True)
        top_n = sorted_examples[:limit]
        logger.info(
            f"Selected {len(top_n)} Pareto-optimal examples (recall range: {top_n[-1][1]:.1%} to {top_n[0][1]:.1%})"
        )
        return [ex for ex, _score in top_n]

    db: Database = ctx.obj

    # 1. Select agent definition to improve
    console.print("[dim]Loading agent definition from database...[/dim]")
    with db.session() as session:
        # Auto-select: find definitions with enough training examples, pick best by validation LCB
        # Phase 1: Get all completed critic runs with grader runs on TRAIN split
        critic_runs = (
            session.query(AgentRun)
            .filter(
                AgentRun.type_config["agent_type"].astext == AgentType.CRITIC,
                AgentRun.status == AgentRunStatus.COMPLETED,
            )
            .all()
        )

        # Build index: image_digest -> set of ExampleSpec for examples that have grader runs
        definition_to_examples: dict[str, set[ExampleSpec]] = {}
        for cr in critic_runs:
            critic_config = cr.critic_config()
            example_spec = critic_config.example
            snapshot_slug = example_spec.snapshot_slug
            definition_id = cr.image_digest

            # Check if this snapshot is in TRAIN split
            snapshot = session.query(Snapshot).filter_by(slug=snapshot_slug).first()
            if not snapshot or snapshot.split != Split.TRAIN:
                continue

            # Check if there's a grader run for this critic run
            has_grader = (
                session.query(AgentRun)
                .filter(
                    AgentRun.type_config["agent_type"].astext == AgentType.GRADER,
                    AgentRun.type_config["graded_agent_run_id"].astext == str(cr.agent_run_id),
                )
                .first()
            )
            if has_grader:
                if definition_id not in definition_to_examples:
                    definition_to_examples[definition_id] = set()
                definition_to_examples[definition_id].add(example_spec)

        # Filter to definitions with enough examples
        definition_example_counts = [
            (def_id, len(examples))
            for def_id, examples in definition_to_examples.items()
            if len(examples) >= n_examples
        ]

        if not definition_example_counts:
            console.print(f"[red]Error:[/red] No definitions have {n_examples}+ training examples with grader runs")
            raise typer.Exit(1)

        eligible_definition_ids = {d for d, _ in definition_example_counts}

        # Get validation whole-snapshot stats for eligible definitions
        valid_whole_stats = (
            session.query(RecallByDefinitionSplitKind)
            .filter(
                RecallByDefinitionSplitKind.split == Split.VALID,
                RecallByDefinitionSplitKind.example_kind == ExampleKind.WHOLE_SNAPSHOT,
                RecallByDefinitionSplitKind.critic_image_digest.in_(eligible_definition_ids),
            )
            .all()
        )

        # Filter to those with at least one successful run
        eligible_stats = [
            s for s in valid_whole_stats if s.status_counts and s.status_counts.get(AgentRunStatus.COMPLETED, 0) > 0
        ]

        if not eligible_stats:
            console.print(f"[red]Error:[/red] No prompts with {n_examples}+ training examples have validation results")
            raise typer.Exit(1)

        # Pick best by validation LCB (whole-snapshot)
        # TODO: Move sorting to SQL side using (recall_stats).lcb95 for efficiency
        def get_lcb(s: RecallByDefinitionSplitKind) -> float:
            if s.recall_stats and s.recall_stats.lcb95 is not None:
                return s.recall_stats.lcb95
            return -1.0

        best = max(eligible_stats, key=get_lcb)
        definition_id = best.critic_image_digest

        example_count = next(count for d, count in definition_example_counts if d == definition_id)
        console.print(f"[green]✓[/green] Selected best definition: {definition_id} ({example_count} training examples)")

        # Display validation stats using fmt_pct1 for percentage formatting
        recall_val = best.recall_stats.mean if best.recall_stats else 0.0
        lcb_val = best.recall_stats.lcb95 if best.recall_stats else None
        n_completed = best.status_counts.get(AgentRunStatus.COMPLETED, 0)
        n_examples_val = best.n_examples or 0
        zero_count = best.zero_count or 0
        timed_out_count = best.status_counts.get(AgentRunStatus.TIMED_OUT, 0)

        console.print(
            f"  Valid (whole_snapshot): recall={fmt_pct(recall_val)}, "
            f"LCB={fmt_pct(lcb_val)}, "
            f"n={n_completed}/{n_examples_val}, "
            f"{zero_count}z {timed_out_count}t"
        )

    # 2. Select training examples
    console.print(f"\n[dim]Selecting {n_examples} training examples...[/dim]")
    with db.session() as session:
        allowed_examples = select_pareto_examples(session, definition_id, n_examples)
        if not allowed_examples:
            console.print("[red]Error:[/red] No training examples found")
            raise typer.Exit(1)

        if len(allowed_examples) < n_examples:
            console.print(
                f"[yellow]Warning:[/yellow] Only {len(allowed_examples)} examples available (requested {n_examples})"
            )

        console.print(f"[green]✓[/green] Selected {len(allowed_examples)} examples")

        table = Table(title="Training Examples")
        table.add_column("Snapshot", style="cyan")
        table.add_column("Scope", style="dim")
        for ex in allowed_examples[:5]:
            # Use isinstance to match discriminated union variants
            if isinstance(ex, WholeSnapshotExample):
                scope_desc = "whole_snapshot"
            elif isinstance(ex, SingleFileSetExample):
                scope_desc = f"file_set_{ex.files_hash}"
            else:
                raise ValueError(f"Unknown example type: {type(ex)}")
            table.add_row(str(ex.snapshot_slug), scope_desc)
        if len(allowed_examples) > 5:
            table.add_row("[dim]...[/dim]", f"[dim](+{len(allowed_examples) - 5} more)[/dim]")
        console.print(table)

    # 3. Run improvement agent
    console.print("\n[bold]Running improvement agent[/bold]")
    console.print(f"  Improvement model: {improvement_model}")
    console.print(f"  Critic model: {critic_model}")
    console.print(f"  Token budget: {token_budget:,}")
    console.print(f"  Examples: {len(allowed_examples)}")
    console.print()

    config = load_config_from_env()
    docker_client = aiodocker.Docker()
    registry = AgentRegistry(
        docker_client,
        db=db,
        db_config=db.config,
        agent_base_env=config.agent_env,
        registry_config=get_registry_proxy_config(),
    )
    try:
        result: ImprovementResult = await registry.run_improvement_agent(
            examples=allowed_examples,
            baseline_image_refs=[definition_id],
            token_budget=token_budget,
            improvement_model=improvement_model,
            critic_model=critic_model,
            timeout_seconds=timeout_seconds,
            output_dir=out_dir,
        )
    except Exception as e:
        console.print(f"\n[red]Error:[/red] {e}")
        logger.exception("Improvement agent failed")
        raise typer.Exit(1)
    finally:
        await registry.close()

    # 5. Display results
    console.print()
    if isinstance(result.outcome, TerminationSuccess):
        panel = Panel(
            f"[green]✓ Improvement agent completed successfully[/green]\n\n"
            f"[bold]Definition ID:[/bold] {result.outcome.definition_id}\n\n"
            f"[bold]Tokens:[/bold] {result.tokens_used:,} / {token_budget:,} "
            f"({100 * result.tokens_used / token_budget:.1f}%)\n\n"
            f"[bold]Total credit:[/bold] {result.outcome.total_credit:.1f}\n"
            f"[bold]Baseline avg:[/bold] {result.outcome.baseline_avg:.1f}",
            title="Improvement Result",
            border_style="green",
        )
        console.print(panel)
    elif isinstance(result.outcome, OutcomeExhausted):
        panel = Panel(
            f"[yellow]! Token budget exhausted[/yellow]\n\n"
            f"[bold]Tokens:[/bold] {result.tokens_used:,} / {token_budget:,} "
            f"({100 * result.tokens_used / token_budget:.1f}%)\n\n"
            f"The agent exhausted its token budget without submitting a prompt. "
            f"Try increasing --token-budget or reducing --n-examples.",
            title="Improvement Result",
            border_style="yellow",
        )
        console.print(panel)
    elif isinstance(result.outcome, OutcomeUnexpectedTermination):
        panel = Panel(
            f"[red]✗ Unexpected termination[/red]\n\n"
            f"[bold]Tokens:[/bold] {result.tokens_used:,} / {token_budget:,} "
            f"({100 * result.tokens_used / token_budget:.1f}%)\n\n"
            f"[bold]Message:[/bold] {result.outcome.message}",
            title="Improvement Result",
            border_style="red",
        )
        console.print(panel)

    console.print()


# GEPA command (optional - requires gepa package)
try:
    from props.cli.cmd_gepa import cmd_gepa

    app.command("gepa")(cmd_gepa)
except ImportError:
    pass

# Stats command group
app.add_typer(stats_app, name="stats")


# ---------- Shared helpers for run ----------


@app.command("run")
@async_run
async def cmd_run(
    ctx: typer.Context,
    # Scope (required)
    snapshot: SnapshotSlug = opt.ARG_SNAPSHOT,
    # Definition ID (required)
    definition_id: DefinitionId = opt.OPT_DEFINITION_ID,
    # File filtering
    files: list[str] | None = opt.OPT_FILES_FILTER,
    # Common options
    model: str = opt.OPT_MODEL,
    timeout_seconds: int = opt.OPT_TIMEOUT_SECONDS,
) -> None:
    """Run critic agent on a snapshot with DB persistence.

    Uses AgentHandle to load agent package from DB. The package's /init script
    outputs the system prompt.

    Example:
        props run ducktape/2025-11-26-00 --definition-id critic
    """
    db: Database = ctx.obj
    config = load_config_from_env()
    docker_client = aiodocker.Docker()

    registry = AgentRegistry(
        docker_client,
        db=db,
        db_config=db.config,
        agent_base_env=config.agent_env,
        registry_config=get_registry_proxy_config(),
    )
    try:
        # Get available files from database (no hydration)
        with db.session() as session:
            snapshot_obj = session.query(Snapshot).filter_by(slug=snapshot).one()
            available_files = snapshot_obj.files_with_issues()

        # Create example spec from file filter
        available_files_dict = dict.fromkeys(available_files)
        example_spec = make_example_from_files(snapshot, available_files_dict, files)

        # Run critic via registry
        critic_run_id = await registry.run_critic(
            image_ref=definition_id,  # definition_id is actually an image ref
            example=example_spec,
            model=model,
            timeout_seconds=timeout_seconds,
            parent_run_id=None,
            budget_usd=None,
        )

        # Print results
        typer.echo("\n=== Critique Complete ===")
        typer.echo(f"Critic Run ID: {critic_run_id}")

        with db.session() as session:
            critic_run = session.get(AgentRun, critic_run_id)
            if critic_run is None:
                raise RuntimeError(f"Critic run {critic_run_id} not found in database")

            if critic_run.status == AgentRunStatus.COMPLETED:
                issues = session.query(ReportedIssue).filter_by(agent_run_id=critic_run_id).all()
                typer.echo(f"Issues found: {len(issues)}")
                for issue in issues:
                    typer.echo(f"\n[{issue.issue_id}] {issue.rationale}")
                    for occ in issue.occurrences:
                        for loc in occ.locations:
                            loc_str = loc.file
                            if loc.start_line:
                                loc_str += f":{loc.start_line}"
                                if loc.end_line and loc.end_line != loc.start_line:
                                    loc_str += f"-{loc.end_line}"
                            typer.echo(f"  - {loc_str}")
            else:
                typer.echo(f"Critic run ended with status: {critic_run.status.value}", err=True)
    finally:
        await registry.close()


if __name__ == "__main__":
    app()
