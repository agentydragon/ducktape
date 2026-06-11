"""GEPA adapter for props critic optimization.

Integrates with gepa-ai/gepa to optimize the critic system prompt using
evolutionary search with rich feedback from execution traces and grader output.

Usage:
    import tempfile
    from pathlib import Path
    from gepa import optimize
    from props.core.gepa.gepa_adapter import CriticAdapter, load_datasets

    # Create scoped temporary directory for this optimization run
    run_dir = Path(tempfile.mkdtemp(prefix="gepa_run_"))

    adapter = CriticAdapter(
        hydrator, critic_client, grader_client, db_config,
        run_dir=run_dir,
        reflection_model="claude-sonnet-4.5"
    )
    trainset, valset = await load_datasets()  # Loads from database

    result = optimize(
        seed_candidate={"system_prompt": initial_prompt},
        trainset=trainset,
        valset=valset,
        adapter=adapter,
        reflection_lm=reflection_lm,
        max_metric_calls=100,
    )
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import logging
import pickle
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import aiodocker
import gepa
import litellm
from gepa.core.result import GEPAResult
from gepa.strategies.instruction_proposal import InstructionProposalSignature
from pydantic import BaseModel
from sqlalchemy import func

from agent_core.events import EventType
from openai_utils.model import OpenAIModelProto
from props.core.agent_types import AgentType
from props.core.agent_workspace import WorkspaceManager
from props.core.display import short_sha
from props.core.gepa.warm_start import build_historical_gepa_state
from props.core.splits import Split
from props.db.config import DatabaseConfig
from props.db.examples import Example, get_examples_for_split
from props.db.models import AgentRun, AgentRunStatus, RecallByDefinitionExample
from props.db.session import get_session
from props.db.snapshots import DBCriticSubmitPayload

logger = logging.getLogger(__name__)


def _gepa_not_implemented() -> None:
    """Raise NotImplementedError for GEPA - called at runtime entrypoints."""
    raise NotImplementedError(
        "GEPA is broken: run_critic_legacy() has been removed. "
        "GEPA needs migration to definition-based run_critic(). "
        "See docs/design/agent-definitions.md Task 9."
    )


# =============================================================================
# Event Filtering
# =============================================================================


def _filter_reflection_events(agent_run_id: UUID) -> list[EventType]:
    """Return empty events list (events table deprecated).

    Events are no longer stored in the database. Execution traces are now
    captured via container logs and llm_requests table instead.
    """
    return []


# =============================================================================
# Data Types
# =============================================================================


@dataclass
class CriticTrajectory:
    """Execution trajectory for a critic run.

    critique_payload is None if the critic ran out of turns before submitting.
    """

    agent_run_id: UUID
    events: list[EventType]
    critique_payload: DBCriticSubmitPayload | None


@dataclass
class CriticOutput:
    """Output from a critic evaluation for GEPA.

    Stores status enums instead of JSONB output unions.
    Semantic data (issues, grading decisions) lives in normalized tables.
    """

    critic_status: AgentRunStatus
    grader_status: AgentRunStatus | None
    critic_run_id: UUID


@dataclass
class EvaluationResult:
    """Result from evaluating a single specimen."""

    output: CriticOutput
    score: float
    trajectory: CriticTrajectory | None


class ReflectionExample(BaseModel):
    """Example for GEPA's reflection dataset.

    Includes both successful critiques and max_turns_exceeded cases.
    Status enums indicate whether critic/grader succeeded.
    grader_status is None when critic exceeded max turns.
    """

    component_name: str
    current_text: str
    score: float
    trajectory: CriticTrajectory
    critic_status: AgentRunStatus
    grader_status: AgentRunStatus | None


# =============================================================================
# GEPA Adapter
# =============================================================================


class CriticAdapter(gepa.GEPAAdapter[Example, CriticTrajectory, CriticOutput]):
    """GEPA adapter for the props critic.

    Implements the GEPAAdapter protocol to allow GEPA to optimize
    the critic system prompt using your existing infrastructure.

    DataInst Type and Checkpointing:
    --------------------------------
    DataInst = Example (snapshot_slug + scope + scope_hash)

    GEPA's ListDataLoader maps Example → integer DataId via list position.
    Checkpoints store scores keyed by these integers: {0: 0.85, 2: 0.90, ...}

    For warm-start to work, load_datasets() MUST return datasets in deterministic
    order across all runs. This is enforced via:
    - get_examples_for_split() orders by (snapshot_slug, scope_hash)

    See warm_start.py for checkpoint reconstruction from historical database runs.
    """

    def __init__(
        self,
        critic_client: OpenAIModelProto,
        grader_client: OpenAIModelProto,
        db_config: DatabaseConfig,
        workspace_manager: WorkspaceManager,
        run_dir: Path,
        reflection_model: str | None = None,
        verbose: bool = False,
        max_parallelism: int = 20,
    ):
        _gepa_not_implemented()
        self.critic_client = critic_client
        self.grader_client = grader_client
        self.db_config = db_config
        self.workspace_manager = workspace_manager
        self.reflection_model = reflection_model
        self.verbose = verbose
        self.max_parallelism = max_parallelism

        # Set up proposal logging if reflection_model provided
        if reflection_model:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            log_file = run_dir / f"gepa_proposals_{timestamp}.jsonl"
            self._setup_proposal_logging(log_file)
            logger.info(f"GEPA proposal logging enabled: {log_file.absolute()}")
        else:
            # No reflection model - GEPA will use default proposal mechanism
            self.propose_new_texts = None

    def _make_evaluation_result(self, example: Example, critic_run_id: UUID, capture_traces: bool) -> EvaluationResult:
        """Build EvaluationResult by querying recall_by_definition_example view.

        Score is computed across ALL runs for this (definition, model, example), not just
        the current run. This gives a stable score reflecting historical performance.

        Args:
            example: Example being evaluated (contains snapshot_slug, example_kind, files_hash)
            critic_run_id: Critic run UUID (for trajectory and status)
            capture_traces: Whether to fetch and build trajectory

        Returns:
            EvaluationResult with aggregate recall score from the view
        """
        # Fetch critic run info
        with get_session() as session:
            critic_run = session.get(AgentRun, critic_run_id)
            if critic_run is None:
                raise ValueError(f"AgentRun {critic_run_id} not found")
            critic_status = critic_run.status
            critic_image_digest = critic_run.image_digest
            critic_model = critic_run.model

        # Build trajectory if requested
        trajectory: CriticTrajectory | None = None
        if capture_traces:
            critique_payload_db: DBCriticSubmitPayload | None = None
            if critic_status == AgentRunStatus.COMPLETED:
                # Notes are no longer stored; empty payload indicates successful completion
                critique_payload_db = DBCriticSubmitPayload(notes_md=None)

            filtered_events = _filter_reflection_events(critic_run_id)
            trajectory = CriticTrajectory(
                agent_run_id=critic_run_id, events=filtered_events, critique_payload=critique_payload_db
            )

        # Query recall_by_definition_example view for aggregate score across all runs
        with get_session() as session:
            recall_row = (
                session.query(RecallByDefinitionExample)
                .filter(
                    RecallByDefinitionExample.critic_image_digest == critic_image_digest,
                    RecallByDefinitionExample.critic_model == critic_model,
                    RecallByDefinitionExample.snapshot_slug == example.snapshot_slug,
                    RecallByDefinitionExample.example_kind == example.example_kind,
                    RecallByDefinitionExample.files_hash == example.files_hash,
                )
                .first()
            )

            score = recall_row.recall_stats.mean if recall_row and recall_row.recall_stats else 0.0

        return EvaluationResult(
            output=CriticOutput(critic_status=critic_status, grader_status=None, critic_run_id=critic_run_id),
            score=score,
            trajectory=trajectory,
        )

    def _setup_proposal_logging(self, log_file: Path) -> None:
        """Set up logging of GEPA's proposal step (reflection LM calls).

        Replaces GEPA's default propose_new_texts implementation with a logging wrapper
        that replicates the exact same behavior but logs all LLM calls to a JSONL file.

        This method sets self.propose_new_texts to a custom function that:
        - Uses InstructionProposalSignature (same as GEPA's default)
        - Calls litellm.completion with self.reflection_model (same as GEPA does)
        - Logs input (prompt, current instruction, feedback) and output (new instruction)

        Args:
            log_file: Path to JSONL file where proposal calls will be logged

        Example log entry format:
            {"timestamp": "2025-01-15T10:30:00", "call_id": 1, "component": "system_prompt",
             "type": "input", "current_instruction": "...", "feedback_count": 3, "prompt": "..."}
            {"timestamp": "2025-01-15T10:30:05", "call_id": 1, "component": "system_prompt",
             "type": "output", "raw_response": "...", "new_instruction": "..."}
        """
        call_count = 0

        def propose_new_texts(
            candidate: dict[str, str],
            reflective_dataset: Mapping[str, Sequence[Mapping[str, Any]]],
            components_to_update: list[str],
        ) -> dict[str, str]:
            """Custom propose_new_texts that replicates GEPA's default with logging.

            Mirrors the implementation in gepa.proposer.reflective_mutation.reflective_mutation
            but adds structured logging before/after each LLM call.
            """
            nonlocal call_count
            new_texts: dict[str, str] = {}

            for name in components_to_update:
                # Skip if no data (same as GEPA does)
                if name not in reflective_dataset or not reflective_dataset.get(name):
                    continue

                call_count += 1
                base_instruction = candidate[name]
                dataset_with_feedback = reflective_dataset[name]

                # Build the prompt (same as InstructionProposalSignature.run does)
                input_dict = {
                    "current_instruction_doc": base_instruction,
                    "dataset_with_feedback": dataset_with_feedback,
                    "prompt_template": None,  # Uses default template
                }
                full_prompt = InstructionProposalSignature.prompt_renderer(input_dict)

                # Log input
                with log_file.open("a") as f:
                    f.write(
                        json.dumps(
                            {
                                "timestamp": datetime.now().isoformat(),
                                "call_id": call_count,
                                "component": name,
                                "type": "input",
                                "current_instruction": base_instruction,
                                "feedback_count": len(dataset_with_feedback),
                                "prompt": full_prompt,
                            }
                        )
                        + "\n"
                    )

                # Call LLM (same as GEPA does when reflection_lm is a string)
                completion = litellm.completion(
                    model=self.reflection_model, messages=[{"role": "user", "content": full_prompt}]
                )
                lm_out = (completion.choices[0].message.content or "").strip()

                # Extract the new instruction (same as InstructionProposalSignature does)
                result = InstructionProposalSignature.output_extractor(lm_out)
                new_instruction = result["new_instruction"]

                # Log output
                with log_file.open("a") as f:
                    f.write(
                        json.dumps(
                            {
                                "timestamp": datetime.now().isoformat(),
                                "call_id": call_count,
                                "component": name,
                                "type": "output",
                                "raw_response": lm_out,
                                "new_instruction": new_instruction,
                            }
                        )
                        + "\n"
                    )

                new_texts[name] = new_instruction

            return new_texts

        self.propose_new_texts = propose_new_texts

    def evaluate(
        self, batch: list[Example], candidate: dict[str, str], capture_traces: bool = False
    ) -> gepa.EvaluationBatch[CriticTrajectory, CriticOutput]:
        """Evaluate a prompt candidate on a batch of specimens.

        Args:
            batch: List of Example to evaluate
            candidate: {"system_prompt": "..."} - the prompt to evaluate
            capture_traces: Whether to capture execution traces

        Returns:
            EvaluationBatch with outputs, scores, and optional trajectories
        """

        # GEPA's evaluate() is synchronous, but our implementation is async
        # Run async code in a new thread with its own event loop to avoid conflicts
        # Create Docker client in the new loop to avoid cross-loop issues
        async def run_in_new_loop_async():
            docker_client = aiodocker.Docker()
            try:
                return await self._evaluate_async(batch, candidate, capture_traces, docker_client)
            finally:
                await docker_client.close()

        def run_in_new_loop():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                return loop.run_until_complete(run_in_new_loop_async())
            finally:
                loop.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(run_in_new_loop)
            results = future.result()

        outputs = [r.output for r in results]
        scores = [r.score for r in results]

        trajectories: list[CriticTrajectory] | None
        if capture_traces:
            # When capture_traces=True, trajectories must be present
            trajectories_with_nones = [r.trajectory for r in results]
            assert all(t is not None for t in trajectories_with_nones), (
                "Trajectories must be present when capture_traces=True"
            )
            trajectories = [t for t in trajectories_with_nones if t is not None]
        else:
            trajectories = None

        return gepa.EvaluationBatch(outputs=outputs, scores=scores, trajectories=trajectories)

    async def _evaluate_one_specimen(
        self,
        specimen_input: Example,
        prompt_sha256: str,
        capture_traces: bool,
        semaphore: asyncio.Semaphore,
        docker_client: aiodocker.Docker,
    ) -> EvaluationResult:
        """Evaluate a single specimen (for parallel execution).

        Uses semaphore to limit concurrent critic/grader runs.
        """
        async with semaphore:
            return await self._evaluate_one_specimen_impl(specimen_input, prompt_sha256, capture_traces, docker_client)

    async def _evaluate_one_specimen_impl(
        self, specimen_input: Example, prompt_sha256: str, capture_traces: bool, docker_client: aiodocker.Docker
    ) -> EvaluationResult:
        """Implementation of single specimen evaluation (called under semaphore)."""
        # NOTE: This code is unreachable because CriticAdapter.__init__ calls _gepa_not_implemented()
        # Keeping signature for type checking purposes only
        _gepa_not_implemented()
        raise AssertionError("unreachable")

    async def _evaluate_async(
        self, batch: list[Example], candidate: dict[str, str], capture_traces: bool, docker_client: aiodocker.Docker
    ) -> list[EvaluationResult]:
        """Async implementation with database-backed caching.

        Three phases:
        1. Check cache: Query for existing (prompt_sha256, snapshot_slug, scope_hash)
        2. Evaluate uncached: Run critic+grader only for cache misses
        3. Reorder results: Return in original batch order

        Semaphore ensures max_parallelism concurrent critic/grader runs.

        Args:
            batch: List of Example to evaluate
            candidate: Prompt candidate dictionary
            capture_traces: Whether to capture execution traces
            docker_client: Docker client created in this event loop
        """
        # Create semaphore for this evaluation batch (scoped to this event loop)
        semaphore = asyncio.Semaphore(self.max_parallelism)

        system_prompt = candidate["system_prompt"]
        # Hash prompt for cache lookup (no DB storage - GEPA is broken anyway)
        prompt_sha256 = hashlib.sha256(system_prompt.encode()).hexdigest()

        # Phase 1: Check DB for each input (single query with LEFT JOIN)
        cached_results: dict[int, EvaluationResult] = {}  # batch_idx -> result found in DB
        uncached_inputs: list[tuple[int, Example]] = []  # (batch_idx, input)

        with get_session() as session:
            # Query completed critic runs matching prompt and model
            critic_runs = (
                session.query(AgentRun)
                .filter(
                    AgentRun.type_config["agent_type"].astext == AgentType.CRITIC,
                    AgentRun.type_config["prompt_sha256"].astext == prompt_sha256,
                    AgentRun.model == self.critic_client.model,
                    AgentRun.status == AgentRunStatus.COMPLETED,
                )
                .all()
            )

            # Index critics by their frozen ExampleSpec (hashable discriminated union)
            critic_by_key = {c.critic_config().example: c for c in critic_runs}

            # Note: Grader lookup disabled - the new daemon model grades all critiques per snapshot,
            # not individual critic runs. This causes cache misses for graded runs.
            # TODO: Implement snapshot-based grader lookup if needed for GEPA caching.
            grader_by_critic_id: dict[UUID, AgentRun] = {}

            # Process each specimen using indexed results
            for idx, specimen_input in enumerate(batch):
                # Use frozen ExampleSpec as cache key (hashable discriminated union)
                cache_key = specimen_input.to_example_spec()

                critic_run = critic_by_key.get(cache_key)
                if not critic_run:
                    logger.info(
                        f"Cache MISS: {specimen_input.snapshot_slug} (prompt={short_sha(prompt_sha256)}, example_kind={specimen_input.example_kind})"
                    )
                    uncached_inputs.append((idx, specimen_input))
                    continue

                # Use critic_run_id directly for events/trajectory
                critic_run_id = critic_run.agent_run_id

                # Check if grader run is required but missing
                grader_run = grader_by_critic_id.get(critic_run_id)
                if critic_run.status == AgentRunStatus.COMPLETED and not grader_run:
                    logger.info(
                        f"Cache MISS (no grader): {specimen_input.snapshot_slug} (prompt={short_sha(prompt_sha256)}, example_kind={specimen_input.example_kind})"
                    )
                    uncached_inputs.append((idx, specimen_input))
                    continue

                # Cache hit - use aggregate recall from view
                logger.info(
                    f"Cache HIT: {specimen_input.snapshot_slug} (prompt={short_sha(prompt_sha256)}, example_kind={specimen_input.example_kind})"
                )
                cached_results[idx] = self._make_evaluation_result(specimen_input, critic_run_id, capture_traces)

        # Phase 2: Evaluate uncached inputs in parallel
        fresh_results: dict[int, EvaluationResult] = {}
        if uncached_inputs:
            tasks = [
                asyncio.create_task(
                    self._evaluate_one_specimen(specimen_input, prompt_sha256, capture_traces, semaphore, docker_client)
                )
                for _, specimen_input in uncached_inputs
            ]
            try:
                evaluated = await asyncio.gather(*tasks)
            except (KeyboardInterrupt, asyncio.CancelledError):
                # Cancel all tasks on interrupt to ensure clean shutdown
                for task in tasks:
                    if not task.done():
                        task.cancel()
                # Wait for all tasks to actually cancel
                await asyncio.gather(*tasks, return_exceptions=True)
                raise

            for (batch_idx, _), result in zip(uncached_inputs, evaluated, strict=True):
                fresh_results[batch_idx] = result

        # Phase 3: Reorder results to match original batch
        results: list[EvaluationResult] = []
        for idx in range(len(batch)):
            if idx in cached_results:
                results.append(cached_results[idx])
            elif idx in fresh_results:
                results.append(fresh_results[idx])
            else:
                raise RuntimeError(f"Missing result for batch index {idx}")

        logger.info(
            f"Evaluation complete: {len(cached_results)} cached, {len(fresh_results)} fresh, {len(results)} total"
        )

        return results

    def make_reflective_dataset(
        self,
        candidate: dict[str, str],
        eval_batch: gepa.EvaluationBatch[CriticTrajectory, CriticOutput],
        components_to_update: list[str],
    ) -> Mapping[str, Sequence[Mapping[str, Any]]]:
        """Build reflective dataset for GEPA's teacher model.

        For each component being optimized, returns a list of examples
        showing what happened and what should be improved.

        Currently only supports optimizing 'system_prompt' component.
        """
        # Validate that we only received supported components
        unsupported = [c for c in components_to_update if c != "system_prompt"]
        if unsupported:
            raise ValueError(
                f"Unsupported components for optimization: {unsupported}. Only 'system_prompt' is supported."
            )

        # GEPA always calls this with capture_traces=True, so trajectories must exist
        assert eval_batch.trajectories is not None, "make_reflective_dataset requires trajectories"

        # Since we only support system_prompt component, process it directly
        examples: list[Mapping[str, Any]] = []
        for output, score, trajectory in zip(
            eval_batch.outputs, eval_batch.scores, eval_batch.trajectories, strict=True
        ):
            # Include both successful critiques and max_turns_exceeded cases
            # grader_status is None when critic exceeded max turns
            example = ReflectionExample(
                component_name="system_prompt",
                current_text=candidate["system_prompt"],
                score=score,
                trajectory=trajectory,
                critic_status=output.critic_status,
                grader_status=output.grader_status,
            )

            examples.append(example.model_dump())

        return {"system_prompt": examples}


# =============================================================================
# Dataset Loading
# =============================================================================


async def load_datasets() -> tuple[list[Example], list[Example]]:
    """Load train and validation datasets for GEPA from database.

    Uses the shared datapoints module for consistent filtering logic across
    train/valid/test splits.

    Training set: All critic scopes (per-file + full-specimen) for tighter feedback loops
    Validation set: Only full-specimen scopes to measure terminal goal (comprehensive review)

    Returns:
        (trainset, valset) tuple of Example lists

    Raises:
        ValueError: If any snapshot has no critic scopes
    """
    logger.info("Loading training examples from examples table")

    with get_session() as session:
        # Use shared datapoints module for consistent filtering logic
        trainset = get_examples_for_split(session, Split.TRAIN)
        valset = get_examples_for_split(session, Split.VALID)

    logger.info(f"Loaded {len(trainset)} training examples, {len(valset)} validation examples")

    return trainset, valset


# =============================================================================
# Statistics Helpers
# =============================================================================


def _log_run_statistics(critic_model: str, grader_model: str) -> None:
    """Log statistics about critic and grader run statuses (success vs max_turns_exceeded).

    Args:
        critic_model: Critic model name to filter runs
        grader_model: Grader model name to filter runs
    """
    with get_session() as session:
        # Count critic run statuses using SQL aggregation
        critic_status_counts = (
            session.query(AgentRun.status, func.count(AgentRun.agent_run_id))
            .filter(AgentRun.type_config["agent_type"].astext == AgentType.CRITIC, AgentRun.model == critic_model)
            .group_by(AgentRun.status)
            .all()
        )

        # Count grader run statuses using SQL aggregation
        grader_status_counts = (
            session.query(AgentRun.status, func.count(AgentRun.agent_run_id))
            .filter(AgentRun.type_config["agent_type"].astext == AgentType.GRADER, AgentRun.model == grader_model)
            .group_by(AgentRun.status)
            .all()
        )

    # Log critic statistics
    total_critic = sum(count for _, count in critic_status_counts)
    if total_critic > 0:
        logger.info(f"Critic run statistics (model={critic_model}, total={total_critic}):")
        for status, count in sorted(critic_status_counts):
            logger.info(f"  {status}: {count} ({count / total_critic:.1%})")
    else:
        logger.info("No critic runs found")

    # Log grader statistics
    total_grader = sum(count for _, count in grader_status_counts)
    if total_grader > 0:
        logger.info(f"Grader run statistics (model={grader_model}, total={total_grader}):")
        for status, count in sorted(grader_status_counts):
            logger.info(f"  {status}: {count} ({count / total_grader:.1%})")
    else:
        logger.info("No grader runs found")


# =============================================================================
# High-level API
# =============================================================================


async def optimize_with_gepa(
    initial_prompt: str,
    critic_client: OpenAIModelProto,
    grader_client: OpenAIModelProto,
    db_config: DatabaseConfig,
    workspace_manager: WorkspaceManager,
    *,
    reflection_model: str,
    max_metric_calls: int = 100,
    verbose: bool = False,
    warm_start: bool = True,
    max_parallelism: int = 20,
    minibatch_size: int = 3,
    use_merge: bool = True,
    max_merge_invocations: int = 5,
    merge_val_overlap_floor: int = 5,
    seed: int | None = None,
) -> tuple[str, GEPAResult[CriticOutput, Any]]:
    """Optimize critic prompt using GEPA.

    Uses critic scopes from database to generate training examples (one per scope).
    Requires all snapshots to have critic scopes defined (enforced by sync validation).

    GEPA supports two complementary strategies that work together:
    1. Reflective mutation (always enabled): LLM analyzes failures and proposes improvements
    2. Merge (optional): Genetic crossover of successful prompt variants

    Args:
        initial_prompt: Starting system prompt (ignored if warm_start=True and historical data exists)
        hydrator: SnapshotHydrator instance for source code hydration
        critic_client: LLM client for critic execution
        grader_client: LLM client for grader execution
        db_config: Database configuration for critic/grader runs
        reflection_model: Model for GEPA's reflection
        max_metric_calls: Budget for evaluations in this run (not counting historical)
        verbose: Enable verbose logging
        warm_start: Load historical Pareto frontier from database (default: True)
        max_parallelism: Maximum concurrent critic/grader runs (default: 20)
        minibatch_size: Number of training examples per reflection iteration (default: 3)
        use_merge: Enable genetic merging of successful variants (default: True)
        max_merge_invocations: Maximum number of merge attempts (default: 5)
        merge_val_overlap_floor: Minimum validation overlap for merge candidates (default: 5)
        seed: Random seed for reproducibility (default: None, uses GEPA default of 0)

    Returns:
        (optimized_prompt, gepa_results) tuple

    Raises:
        ValueError: If any snapshot has no critic scopes
    """
    _gepa_not_implemented()
    logger.info("Starting GEPA optimization")
    logger.info(f"Reflection model: {reflection_model}")
    logger.info(f"Max metric calls: {max_metric_calls}")
    logger.info(f"Minibatch size: {minibatch_size}")
    logger.info(f"Initial prompt length: {len(initial_prompt)} chars")
    logger.info(f"Warm start: {warm_start}")
    if seed is not None:
        logger.info(f"Random seed: {seed}")

    # Load datasets (always uses critic scopes from database)
    logger.info("Loading datasets...")
    trainset, valset = await load_datasets()
    logger.info(f"Loaded {len(trainset)} training examples, {len(valset)} validation examples")

    # Prepare run directory with optional warm-start checkpoint
    run_dir = None
    if warm_start:
        logger.info("Building historical GEPA state from database...")
        historical_state = build_historical_gepa_state(
            valset=valset, critic_model=critic_client.model, grader_model=grader_client.model
        )

        if historical_state:
            # Create temp directory and save checkpoint
            temp_dir = tempfile.mkdtemp(prefix="gepa_warm_start_")
            checkpoint_path = Path(temp_dir) / "gepa_state.bin"
            with checkpoint_path.open("wb") as f:
                pickle.dump(historical_state, f)
            logger.info(
                f"Saved historical state with {len(historical_state['program_candidates'])} prompts to {checkpoint_path}"
            )
            run_dir = temp_dir
        else:
            logger.warning("No historical data found - starting from seed candidate")

    # If no run_dir yet (no warm start or no historical data), create one
    if run_dir is None:
        run_dir = tempfile.mkdtemp(prefix="gepa_run_")
        logger.info(f"Created run directory: {run_dir}")

    # Create adapter
    logger.info(f"Creating CriticAdapter with max_parallelism={max_parallelism}")
    adapter = CriticAdapter(
        critic_client,
        grader_client,
        db_config,
        workspace_manager,
        Path(run_dir),
        reflection_model=reflection_model,
        verbose=verbose,
        max_parallelism=max_parallelism,
    )

    # Run optimization (reflection_lm accepts model string directly)
    logger.info(f"Starting GEPA evolutionary search (merge={'enabled' if use_merge else 'disabled'})...")
    result: GEPAResult[CriticOutput, Any] = gepa.optimize(
        seed_candidate={"system_prompt": initial_prompt},
        trainset=trainset,
        valset=valset,
        adapter=adapter,
        reflection_lm=reflection_model,
        max_metric_calls=max_metric_calls,
        perfect_score=1.0,  # Perfect recall
        run_dir=run_dir,  # Load checkpoint if provided
        reflection_minibatch_size=minibatch_size,
        use_merge=use_merge,
        max_merge_invocations=max_merge_invocations,
        merge_val_overlap_floor=merge_val_overlap_floor,
        seed=seed if seed is not None else 0,
    )

    optimized_prompt = result.best_candidate["system_prompt"]
    best_score = result.val_aggregate_scores[result.best_idx]
    logger.info(f"GEPA optimization complete. Best score: {best_score:.3f}, Metric calls: {result.total_metric_calls}")

    # Log run statistics (critic/grader status breakdown)
    _log_run_statistics(critic_client.model, grader_client.model)

    return optimized_prompt, result
