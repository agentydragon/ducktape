"""Grade validation set: ensure complete critic coverage across all definitions."""

from __future__ import annotations

import asyncio
import logging
import traceback
from collections import defaultdict
from uuid import UUID

import aiodocker
import typer

from cli_util.decorators import async_run
from props.cli import common_options as opt
from props.cli.resources import get_database_config
from props.core.agent_types import AgentType
from props.core.display import short_uuid
from props.core.models.examples import ExampleKind, ExampleSpec
from props.core.splits import Split
from props.db.examples import Example
from props.db.models import AgentDefinition, AgentRun, AgentRunStatus, RecallByDefinitionSplitKind, Snapshot
from props.db.session import get_session
from props.orchestration.agent_registry import AgentRegistry

logger = logging.getLogger(__name__)


@async_run
async def cmd_grade_validation(
    critic_model: str = opt.OPT_CRITIC_MODEL,
    max_parallel: int = opt.OPT_MAX_PARALLEL,
    llm_proxy_url: str = opt.OPT_LLM_PROXY_URL,
    timeout_seconds: int = opt.OPT_TIMEOUT_SECONDS,
) -> None:
    """Grade validation set: ensure complete critic coverage across all definitions.

    For each validation snapshot example:
    1. For each (example, critic_definition) pair:
       a. Check if successful critic run exists (via AgentRun)
       b. If not, RUN critic to generate it

    Grading is handled automatically by snapshot grader daemons.

    Note: Validation/test snapshots should have exactly one example each (full-specimen scope).
    """

    docker_client = aiodocker.Docker()
    db_config = get_database_config()
    registry = AgentRegistry(docker_client=docker_client, db_config=db_config, llm_proxy_url=llm_proxy_url)
    try:
        # Phase 1: Find all work items (snapshot, scope, prompt) combinations
        with get_session() as session:
            # Query validation examples directly (same logic as stats/datapoints)
            validation_examples = (
                session.query(Example)
                .join(Snapshot, Snapshot.slug == Example.snapshot_slug)
                .where(Snapshot.split == Split.VALID)
                .order_by(Example.snapshot_slug, Example.example_kind, Example.files_hash.nullsfirst())
                .all()
            )

            if not validation_examples:
                typer.echo("No validation examples found")
                return

            typer.echo(f"Found {len(validation_examples)} validation examples")

            # Get all critic definitions ordered by validation LCB desc
            # Query aggregated view for VALID whole-snapshot stats
            valid_stats = (
                session.query(RecallByDefinitionSplitKind)
                .filter(
                    RecallByDefinitionSplitKind.split == Split.VALID,
                    RecallByDefinitionSplitKind.example_kind == ExampleKind.WHOLE_SNAPSHOT,
                )
                .all()
            )
            # TODO: Move sorting to SQL side using (recall_stats).lcb95 for efficiency
            valid_stats_sorted = sorted(
                valid_stats,
                key=lambda r: r.recall_stats.lcb95 if r.recall_stats and r.recall_stats.lcb95 else -1.0,
                reverse=True,
            )
            ordered_image_digests = [r.critic_image_digest for r in valid_stats_sorted]

            # Also get any critic definitions not yet evaluated (not in perf stats)
            all_critic_defs = (
                session.query(AgentDefinition).filter(AgentDefinition.agent_type == AgentType.CRITIC).all()
            )
            unevaluated_defs = [d.digest for d in all_critic_defs if d.digest not in ordered_image_digests]

            # Combine: evaluated definitions first (in priority order), then unevaluated
            all_image_digests: list[str] = ordered_image_digests + unevaluated_defs

            if not all_image_digests:
                raise typer.BadParameter("No critic definitions found in database - run 'props db sync' first")

            typer.echo(f"Found {len(all_image_digests)} critic definitions\n")

            # Build work items grouped by image digest
            work_items_by_image: dict[str, list[Example]] = defaultdict(list)

            for example in validation_examples:
                for image_digest in all_image_digests:
                    # Check if successful critic run exists for (example, definition)
                    critic_run = (
                        session.query(AgentRun)
                        .filter(
                            AgentRun.image_digest == image_digest,
                            AgentRun.type_config["example"]["snapshot_slug"].astext == example.snapshot_slug,
                            AgentRun.type_config["example"]["kind"].astext == example.example_kind.value,
                            AgentRun.status == AgentRunStatus.COMPLETED,
                        )
                        .order_by(AgentRun.created_at.desc())
                        .first()
                    )

                    if critic_run is None:
                        # No successful critic run exists, need to run critic
                        work_items_by_image[image_digest].append(example)

            # Count work needed
            total_pairs = len(validation_examples) * len(all_image_digests)
            all_examples = [ex for examples in work_items_by_image.values() for ex in examples]
            need_critic = len(all_examples)
            completed = total_pairs - need_critic

            typer.echo("\nWork summary:")
            typer.echo(f"  {need_critic} items need critic runs")
            typer.echo(f"  {completed} items complete ({completed}/{total_pairs})")

            if need_critic == 0:
                typer.echo("\n✓ All validation set examples have complete critic coverage!")
                return

        # Phase 2: Process definitions with worker pool
        typer.echo(f"\n=== Processing {need_critic} items with {max_parallel} workers ===\n")

        async def process_one(
            example: ExampleSpec, image_digest: str, worker_id: int, item_index: int, total_items: int
        ) -> tuple[str, bool, UUID | None]:
            """Process one work item: run critic.
            Returns (status, success, critic_run_id)."""
            snapshot_slug = example.snapshot_slug

            try:
                # Run critic using registry
                critic_run_id = await registry.run_critic(
                    image_ref=image_digest,
                    example=example,
                    model=critic_model,
                    timeout_seconds=timeout_seconds,
                    parent_run_id=None,
                    budget_usd=None,
                )

                # Check if critic succeeded
                with get_session() as session:
                    critic_run = session.get(AgentRun, critic_run_id)
                    assert critic_run is not None
                    status = critic_run.status

                if status != AgentRunStatus.COMPLETED:
                    typer.echo(
                        f"[W{worker_id} {item_index}/{total_items}] ⚠ Critic {status}: {snapshot_slug} x {image_digest}"
                    )
                    return (status.value, False, critic_run_id)

                typer.echo(
                    f"[W{worker_id} {item_index}/{total_items}] ✓ Critic {snapshot_slug} x {image_digest} → {short_uuid(critic_run_id)}"
                )
                return ("complete", True, critic_run_id)

            except Exception as e:
                typer.echo(
                    f"[W{worker_id} {item_index}/{total_items}] ✗ Critic failed {snapshot_slug} x {image_digest}: {e}\n"
                    f"{traceback.format_exc()}",
                    err=True,
                )
                return ("critic_failed", False, None)

        # Worker pool: process (definition, example) pairs with queue
        all_results: list[tuple[str, bool, UUID | None]] = []
        results_lock = asyncio.Lock()

        # Build queue of (image_digest, Example) tuples
        work_queue: asyncio.Queue[tuple[str, Example]] = asyncio.Queue()
        total_items = 0
        for image_digest in all_image_digests:
            examples = work_items_by_image.get(image_digest, [])
            for example in examples:
                await work_queue.put((image_digest, example))
                total_items += 1

        items_processed = 0
        progress_lock = asyncio.Lock()

        async def worker(worker_id: int) -> None:
            """Worker that grabs (image_digest, example) items from queue and processes them."""
            nonlocal items_processed

            while True:
                try:
                    image_digest, example = work_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

                async with progress_lock:
                    items_processed += 1
                    item_index = items_processed

                result = await process_one(example.to_example_spec(), image_digest, worker_id, item_index, total_items)

                async with results_lock:
                    all_results.append(result)

                work_queue.task_done()

        # Run workers
        await asyncio.gather(*[worker(i) for i in range(1, max_parallel + 1)])

        results = all_results

        # Summary
        complete = sum(1 for status, _, _ in results if status == "complete")
        failures = sum(1 for status, _, _ in results if status == "critic_failed")
        other_status = len(results) - complete - failures

        typer.echo("\n=== Final Summary ===")
        typer.echo(f"Complete: {complete}")
        typer.echo(f"Failures: {failures}")
        if other_status > 0:
            typer.echo(f"Other status (max_turns_exceeded, etc.): {other_status}")
        typer.echo("\nGrading is handled automatically by snapshot grader daemons.")
    finally:
        await registry.close()
