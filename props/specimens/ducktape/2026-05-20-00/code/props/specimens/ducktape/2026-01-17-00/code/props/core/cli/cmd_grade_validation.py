"""Grade validation set: ensure complete critic and grader coverage across all definitions."""

from __future__ import annotations

import asyncio
import logging
import traceback
from collections import defaultdict
from dataclasses import dataclass
from uuid import UUID

import aiodocker
import typer
from sqlalchemy import func

from cli_util.decorators import async_run
from openai_utils.client_factory import build_client
from props.core.agent_registry import AgentRegistry
from props.core.agent_types import AgentType
from props.core.agent_workspace import WorkspaceManager
from props.core.cli import common_options as opt
from props.core.cli.resources import get_database_config
from props.core.db.agent_definition_ids import GRADER_IMAGE_REF
from props.core.db.examples import Example
from props.core.db.models import (
    AgentDefinition,
    AgentRun,
    AgentRunStatus,
    GradingEdge,
    RecallByDefinitionSplitKind,
    Snapshot,
)
from props.core.db.session import get_session
from props.core.display import short_uuid
from props.core.models.examples import ExampleKind, ExampleSpec
from props.core.splits import Split

logger = logging.getLogger(__name__)


@dataclass
class ValidationWorkItem:
    """Work item for validation grading."""

    example: ExampleSpec
    parent_agent_run_id: UUID | None


@async_run
async def cmd_grade_validation(
    grader_model: str = opt.OPT_GRADER_MODEL,
    critic_model: str = opt.OPT_CRITIC_MODEL,
    max_parallel: int = opt.OPT_MAX_PARALLEL,
    verbose: bool = opt.OPT_VERBOSE,
) -> None:
    """Grade validation set: ensure complete critic and grader coverage across all definitions.

    For each validation snapshot example:
    1. For each (example, critic_definition) pair:
       a. Check if successful critic run exists (via AgentRun)
       b. If not, RUN critic to generate it
    2. For each successful critic run:
       a. Check if grader run exists (for ANY model)
       b. If not, RUN grader with specified grader_model

    This ensures we have complete evaluation coverage for validation set terminal metrics.

    Note: Validation/test snapshots should have exactly one example each (full-specimen scope).
    """
    docker_client = aiodocker.Docker()
    db_config = get_database_config()
    workspace_manager = WorkspaceManager.from_env()
    registry = AgentRegistry(
        docker_client=docker_client, db_config=db_config, workspace_manager=workspace_manager, max_parallel=max_parallel
    )
    try:
        critic_client = build_client(critic_model)
        grader_client = build_client(grader_model)

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
            # Each image digest gets a list of ValidationWorkItem
            # parent_agent_run_id is None if critic needs to run, otherwise UUID if grader needs to run
            work_items_by_image: dict[str, list[ValidationWorkItem]] = defaultdict(list)

            for example in validation_examples:
                example_spec = example.to_example_spec()
                for image_digest in all_image_digests:
                    # Check if successful critic run exists for (example, definition)
                    # Query AgentRun by image_digest and type_config fields
                    # Note: type_config['example'] is the full ExampleSpec JSON
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
                        # No successful critic run exists, need to run critic then grader
                        work_items_by_image[image_digest].append(
                            ValidationWorkItem(example=example_spec, parent_agent_run_id=None)
                        )
                        continue

                    # Check if successful grader run exists for this critic run
                    # Accept grader runs from ANY model
                    successful_grader_exists = (
                        session.query(AgentRun)
                        .filter(
                            AgentRun.image_digest == GRADER_IMAGE_REF,
                            AgentRun.type_config["graded_agent_run_id"].astext == str(critic_run.agent_run_id),
                            AgentRun.status == AgentRunStatus.COMPLETED,
                        )
                        .first()
                    )

                    if not successful_grader_exists:
                        # Critic succeeded, need to run grader
                        work_items_by_image[image_digest].append(
                            ValidationWorkItem(example=example_spec, parent_agent_run_id=critic_run.agent_run_id)
                        )
                    # else: both critic and grader succeeded - nothing to do

            # Count work needed (flatten to count)
            total_pairs = len(validation_examples) * len(all_image_digests)
            all_work_items = [item for items in work_items_by_image.values() for item in items]
            need_critic = sum(1 for item in all_work_items if item.parent_agent_run_id is None)
            need_grader_only = sum(1 for item in all_work_items if isinstance(item.parent_agent_run_id, UUID))
            completed = total_pairs - len(all_work_items)

            typer.echo("\nWork summary:")
            typer.echo(f"  {need_critic} items need critic + grader")
            typer.echo(f"  {need_grader_only} items need grader only")
            typer.echo(f"  {completed} items complete ({completed}/{total_pairs})")

            if need_critic == 0 and need_grader_only == 0:
                typer.echo("\n✓ All validation set examples have complete coverage!")
                return

        # Phase 2: Process definitions with worker pool, examples within each definition in parallel
        typer.echo(f"\n=== Processing {need_critic + need_grader_only} items with {max_parallel} workers ===\n")

        async def process_one(
            example: ExampleSpec,
            image_digest: str,
            critic_run_id_or_none: UUID | None,
            worker_id: int,
            item_index: int,
            total_items: int,
        ) -> tuple[str, bool, bool, UUID | None]:
            """Process one work item: run critic if needed, then grader.
            Returns (status, critic_success, grader_success, grader_run_id)."""
            critic_run_id = critic_run_id_or_none
            critic_success = True
            grader_success = True
            grader_run_id: UUID | None = None
            snapshot_slug = example.snapshot_slug

            # Step 1: Run critic if needed
            if critic_run_id is None:
                try:
                    # Run critic using registry
                    critic_run_id = await registry.run_critic(
                        image_ref=image_digest, example=example, client=critic_client, verbose=verbose, max_turns=100
                    )

                    # Check if critic succeeded - if not, skip grading
                    with get_session() as session:
                        critic_run = session.get(AgentRun, critic_run_id)
                        assert critic_run is not None
                        status = critic_run.status

                    if status != AgentRunStatus.COMPLETED:
                        # Critic failed (max_turns_exceeded or context_length_exceeded)
                        if not verbose:
                            typer.echo(
                                f"[W{worker_id} {item_index}/{total_items}] ⚠ Critic {status}: {snapshot_slug} x {image_digest}"
                            )
                        return (status, False, False, None)

                    if not verbose:
                        typer.echo(
                            f"[W{worker_id} {item_index}/{total_items}] ✓ Critic {snapshot_slug} x {image_digest} → {short_uuid(critic_run_id)}"
                        )
                except Exception as e:
                    typer.echo(
                        f"[W{worker_id} {item_index}/{total_items}] ✗ Critic failed {snapshot_slug} x {image_digest}: {e}\n"
                        f"{traceback.format_exc()}",
                        err=True,
                    )
                    return ("critic_failed", False, False, None)

            # Step 2: Run grader
            try:
                grader_run_id = await registry.run_grader(
                    critic_run_id=critic_run_id, client=grader_client, verbose=verbose, max_turns=200
                )

                # Fetch recall for progress message (direct query to grading_edges)
                with get_session() as session:
                    grader_run = session.get(AgentRun, grader_run_id)
                    assert grader_run is not None

                    if grader_run.status == AgentRunStatus.COMPLETED:
                        # Show absolute numbers instead of percentage (query grading_edges)
                        total_credit = (
                            session.query(func.sum(GradingEdge.credit))
                            .filter_by(grader_run_id=grader_run_id)
                            .filter(GradingEdge.tp_id.isnot(None))  # Only TP matches
                            .scalar()
                            or 0.0
                        )
                        n_occurrences = (
                            session.query(GradingEdge.tp_id, GradingEdge.tp_occurrence_id)
                            .filter_by(grader_run_id=grader_run_id)
                            .filter(GradingEdge.tp_id.isnot(None))
                            .distinct()
                            .count()
                        )
                        result_str = f"{total_credit:.1f} / {n_occurrences} found"
                    else:
                        result_str = f"status={grader_run.status.value}"

                    if not verbose:
                        typer.echo(
                            f"[W{worker_id} {item_index}/{total_items}] ✓ Graded {short_uuid(critic_run_id)} → {short_uuid(grader_run_id)} "
                            f"({result_str})"
                        )
            except Exception as e:
                typer.echo(
                    f"[W{worker_id} {item_index}/{total_items}] ✗ Grader failed {short_uuid(critic_run_id)}: {e}\n"
                    f"{traceback.format_exc()}",
                    err=True,
                )
                return ("grader_failed", critic_success, False, None)

            return ("complete", critic_success, grader_success, grader_run_id)

        # Worker pool: process (definition, example) pairs with queue
        all_results: list[tuple[str, bool, bool, UUID | None]] = []
        results_lock = asyncio.Lock()

        # Build queue of (image_digest, ValidationWorkItem) tuples
        # Ordered by definition priority (same order as stats table: valid LCB desc, train LCB desc, created_at desc)
        work_queue: asyncio.Queue[tuple[str, ValidationWorkItem]] = asyncio.Queue()
        total_items = 0
        for image_digest in all_image_digests:
            items = work_items_by_image.get(image_digest, [])
            for item in items:
                await work_queue.put((image_digest, item))
                total_items += 1

        items_processed = 0
        progress_lock = asyncio.Lock()

        async def worker(worker_id: int) -> None:
            """Worker that grabs (image_digest, work_item) items from queue and processes them."""
            nonlocal items_processed

            while True:
                try:
                    image_digest, work_item = work_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

                async with progress_lock:
                    items_processed += 1
                    item_index = items_processed

                result = await process_one(
                    work_item.example, image_digest, work_item.parent_agent_run_id, worker_id, item_index, total_items
                )

                async with results_lock:
                    all_results.append(result)

                work_queue.task_done()

        # Run workers
        await asyncio.gather(*[worker(i) for i in range(1, max_parallel + 1)])

        results = all_results

        # Summary
        complete = sum(1 for status, _, _, _ in results if status == "complete")
        critic_failures = sum(1 for status, _, _, _ in results if status == "critic_failed")
        grader_failures = sum(1 for status, _, _, _ in results if status == "grader_failed")

        typer.echo("\n=== Final Summary ===")
        typer.echo(f"Complete: {complete}")
        typer.echo(f"Critic failures: {critic_failures}")
        typer.echo(f"Grader failures: {grader_failures}")
        typer.echo("\nFor recall metrics, query: aggregated_recall_by_definition or aggregated_recall_by_example views")
    finally:
        await registry.close()
