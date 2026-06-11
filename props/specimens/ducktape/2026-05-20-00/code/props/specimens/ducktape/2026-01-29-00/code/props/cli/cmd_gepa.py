"""GEPA optimization command."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from cli_util.decorators import async_run
from openai_utils.client_factory import build_client
from props.cli import common_options as opt
from props.core.agent_workspace import WorkspaceManager
from props.core.gepa.gepa_adapter import optimize_with_gepa
from props.db.config import get_database_config


@async_run
async def cmd_gepa(
    critic_model: str = opt.OPT_CRITIC_MODEL,
    grader_model: str = opt.OPT_GRADER_MODEL,
    reflection_model: str = opt.OPT_OPTIMIZER_MODEL,
    initial_prompt: Annotated[
        str | None, typer.Option(help="Initial prompt (ignored if warm-start loads historical data)")
    ] = None,
    max_metric_calls: Annotated[
        int, typer.Option(help="Budget for evaluations in this run (not counting historical)")
    ] = 100,
    output_dir: Annotated[Path, typer.Option(help="Output directory for results")] = Path("gepa_output"),
    warm_start: Annotated[
        bool, typer.Option(help="Load historical Pareto frontier from database to start from known good prompts")
    ] = True,
    max_parallelism: Annotated[int, typer.Option(help="Maximum concurrent critic/grader evaluations")] = 20,
    minibatch_size: Annotated[int, typer.Option(help="Number of training examples per reflection iteration")] = 3,
    verbose: Annotated[bool, typer.Option(help="Enable verbose logging")] = False,
    seed: Annotated[int | None, typer.Option(help="Random seed for reproducibility (default: timestamp-based)")] = None,
) -> None:
    """Run GEPA optimization to evolve the critic system prompt.

    GEPA (Genetic Prompt Adaptation) uses evolutionary search with rich feedback
    from execution traces and grader output to optimize the critic prompt.

    Example:
        props gepa --critic-model gpt-4o-mini --grader-model gpt-4o --reflection-model gpt-4o
    """
    console = Console()

    # Load initial prompt
    if initial_prompt is None:
        initial_prompt = "You are a code critic."
        console.print(f"[dim]Using default initial prompt: {initial_prompt}[/dim]")

    # Compute seed (timestamp-based if not provided)
    actual_seed = seed if seed is not None else int(time.time())
    console.print(
        f"[dim]Random seed: {actual_seed} {'(user-provided)' if seed is not None else '(timestamp-based)'}[/dim]"
    )

    console.print("\n[bold cyan]GEPA Optimization Configuration[/bold cyan]")
    console.print(f"  Critic model: {critic_model}")
    console.print(f"  Grader model: {grader_model}")
    console.print(f"  Reflection model: {reflection_model}")
    console.print(f"  Max metric calls: {max_metric_calls} (this run only)")
    console.print(f"  Max parallelism: {max_parallelism} concurrent evaluations")
    console.print(f"  Minibatch size: {minibatch_size} training examples per reflection")
    console.print("  Training examples: per-file mode (from database critic_scopes)")
    console.print(f"  Warm start: {'enabled' if warm_start else 'disabled'}")
    console.print(f"  Output directory: {output_dir}")
    console.print(f"  Initial prompt length: {len(initial_prompt)} chars\n")

    # Run optimization
    console.print("\n[bold green]Starting GEPA optimization...[/bold green]\n")
    db_config = get_database_config()
    workspace_manager = WorkspaceManager.from_env()
    optimized_prompt, result = await optimize_with_gepa(
        initial_prompt=initial_prompt,
        critic_client=build_client(critic_model),
        grader_client=build_client(grader_model),
        db_config=db_config,
        workspace_manager=workspace_manager,
        reflection_model=reflection_model,
        max_metric_calls=max_metric_calls,
        max_parallelism=max_parallelism,
        minibatch_size=minibatch_size,
        verbose=verbose,
        warm_start=warm_start,
        seed=actual_seed,
    )

    # Save results
    output_dir.mkdir(exist_ok=True, parents=True)
    optimized_file = output_dir / "optimized_prompt.md"
    optimized_file.write_text(optimized_prompt)

    # Print summary
    best_score = result.val_aggregate_scores[result.best_idx]
    metric_calls = result.total_metric_calls or 0
    console.print("\n" + "=" * 80)
    console.print("[bold green]GEPA Optimization Complete![/bold green]")
    console.print(f"  Best candidate score: [cyan]{best_score:.3f}[/cyan]")
    console.print(f"  Total evaluations: [cyan]{metric_calls}[/cyan]")
    console.print(f"  Optimized prompt saved to: [cyan]{optimized_file.absolute()}[/cyan]")
    console.print("=" * 80 + "\n")
