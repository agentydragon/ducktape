"""PromptEvalServer: MCP server for critic evaluation orchestration.

Used by both prompt optimizer and improvement agents to run critic evaluations.
Provides run_critic and wait_until_graded tools via MCP-over-HTTP.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from fastmcp.exceptions import ToolError
from pydantic import Field
from sqlalchemy import func

from mcp_infra.enhanced.server import EnhancedFastMCP
from mcp_infra.flat_tool import FlatTool
from openai_utils.pydantic_strict_mode import OpenAIStrictModeBaseModel
from props.core.critic.exceptions import CriticExecutionError
from props.core.db.examples import Example
from props.core.db.models import AgentDefinition, AgentRun, AgentRunStatus, GradingEdge, GradingPending, Snapshot
from props.core.db.session import get_session
from props.core.exceptions import AgentDidNotSubmitError
from props.core.ids import DefinitionId, SnapshotSlug
from props.core.models.examples import ExampleKind, ExampleSpec, SingleFileSetExample
from props.core.prompt_optimize.target_metric import TargetMetric
from props.core.splits import Split

if TYPE_CHECKING:
    from props.core.agent_registry import AgentRegistry

logger = logging.getLogger(__name__)


_AGENT_STUCK_ADVICE = (
    "Agent exceeded turn limit. This could mean:\n"
    "  1. Agent needed more turns to complete the task (reading files, analyzing code, etc.)\n"
    "  2. Agent stuck in a loop or not following instructions\n"
    "  3. Agent ran out of tokens\n"
    "Check the transcript in the database to determine if the agent was making productive progress or stuck."
)

_VALIDATION_FUNCTION_NAME = "get_validation_run_aggregates()"

_VALID_TEST_FULL_SNAPSHOT_ONLY = (
    "'valid' split only allows full-snapshot evaluations (example_kind must be 'whole_snapshot'). "
    "Run critic on whole-snapshot examples to measure terminal metric."
)

_FUNCTION_BASED_METRICS_ADVICE = (
    f"To get recall metrics, call the {_VALIDATION_FUNCTION_NAME} SQL function. "
    "This function returns per-run aggregate metrics (total_credit, n_occurrences per run). "
    "You must aggregate across runs manually if needed."
)

_VIEW_BASED_METRICS_ADVICE = (
    "To get recall metrics, query the recall_by_definition_split_kind or recall_by_example views. "
    "These views pre-aggregate occurrence-level credits across multiple runs and include stats (n_examples, n_runs, ucb, lcb)."
)


@dataclass
class PromptOptimizerState:
    error: str | None = None


class ReportFailureInput(OpenAIStrictModeBaseModel):
    message: str = Field(description="Error message explaining why optimization could not be completed")


def _trace_advice_for_run(run_id: UUID, is_grader: bool = False) -> str:
    """Generate trace query advice when we have a concrete run_id."""
    agent_type = "Grader" if is_grader else "Critic"
    return f"""{agent_type} agent run ID: {run_id}

Query examples:
-- Get run details:
SELECT * FROM agent_runs WHERE agent_run_id = '{run_id}';

-- Get execution trace:
SELECT event_type, payload FROM events WHERE agent_run_id = '{run_id}' ORDER BY sequence_num;

-- Get reasoning summaries:
SELECT payload FROM events WHERE agent_run_id = '{run_id}' AND event_type = 'reasoning' ORDER BY sequence_num;"""


def _trace_advice_for_snapshot(snapshot_slug: SnapshotSlug) -> str:
    """Generate trace query advice when we only have snapshot_slug (before run completes)."""
    return f"Query agent_runs WHERE type_config->>'snapshot_slug'='{snapshot_slug}' AND type_config->>'agent_type'='critic' to get run IDs."


class RunCriticInput(OpenAIStrictModeBaseModel):
    definition_id: DefinitionId = Field(
        description="Agent package ID (from 'props agent-pkg create' or 'critic' for baseline)"
    )
    example: ExampleSpec = Field(description="Example to evaluate (WholeSnapshotExample or SingleFileSetExample)")
    timeout_seconds: int = Field(description="Max seconds before container is killed")
    budget_usd: float | None = Field(description="Max USD cost for this agent (enforced by proxy)")


class RunCriticOutput(OpenAIStrictModeBaseModel):
    critic_run_id: UUID = Field(
        description="agent_run_id of the critic agent run. Query agent_runs for output, costs, model. Use wait_until_graded to get grading results."
    )


class WaitUntilGradedInput(OpenAIStrictModeBaseModel):
    critic_run_id: UUID = Field(description="agent_run_id of the critic run to wait for grading")
    timeout_seconds: int = Field(
        default=300, ge=10, le=3600, description="Maximum time to wait for grading (default 300s, max 1 hour)"
    )
    poll_interval_seconds: int = Field(
        default=5, ge=1, le=60, description="How often to check for grading completion (default 5s)"
    )


class WaitUntilGradedOutput(OpenAIStrictModeBaseModel):
    grader_run_id: UUID = Field(description="agent_run_id of the grader run that graded this critic")
    total_credit: float = Field(description="Sum of credits for TP matches (recall numerator)")
    max_credit: int = Field(description="Number of distinct TP occurrences (recall denominator)")
    message: str = Field(description="Query advice for getting aggregate metrics")


class PromptEvalServer(EnhancedFastMCP):
    """MCP server for critic evaluation orchestration.

    Provides tools for running critic agents and waiting for grading results.
    Used by both prompt optimizer and improvement agents.
    """

    RUN_CRITIC_TOOL = "run_critic"
    WAIT_UNTIL_GRADED_TOOL = "wait_until_graded"

    run_critic_tool: FlatTool[Any, Any]
    wait_until_graded_tool: FlatTool[Any, Any]
    report_failure_tool: FlatTool[Any, Any]

    def __init__(
        self,
        *,
        critic_model: str,
        registry: AgentRegistry,
        optimizer_state: PromptOptimizerState,
        target_metric: TargetMetric,
        optimizer_run_id: UUID,
    ):
        super().__init__(
            "prompt_eval",
            instructions=(
                "Agent definition evaluation tools: "
                "run_critic(definition_id, example) - run critic agent on example (blocks until complete), "
                "wait_until_graded(critic_run_id) - wait for grader daemon to grade critic run. "
                "Create packages via CLI: props agent-pkg create /workspace/my_critic/. "
                "Query the database for results, costs, and metrics. "
                "Use report_failure to declare the run unsuccessful and abort."
            ),
        )

        # Store parameters for use in tools
        self._critic_model = critic_model
        self._registry = registry
        self._optimizer_state = optimizer_state
        self._target_metric = target_metric
        self._optimizer_run_id = optimizer_run_id

        async def run_critic(payload: RunCriticInput) -> RunCriticOutput:
            """Run critic agent using an agent package.

            Loads critic package from database and runs the /init script to get
            the system prompt, then runs the critic on the specified example.

            Validates split-based access restrictions:
            - TRAIN split: all example types allowed
            - VALID split: restrictions depend on target_metric mode
            - TEST split: completely off-limits

            Returns critic_run_id for subsequent grading via wait_until_graded.
            """
            # Validate definition exists
            with get_session() as session:
                definition = session.get(AgentDefinition, payload.definition_id)
                if not definition:
                    raise ToolError(
                        f"Agent definition not found: {payload.definition_id}. "
                        f"Use CLI: props agent-pkg create /workspace/my_critic/"
                    )

                # Load and validate snapshot
                snapshot_slug = payload.example.snapshot_slug
                db_snapshot = session.query(Snapshot).filter_by(slug=snapshot_slug).one_or_none()
                if not db_snapshot:
                    raise ToolError(f"Snapshot {snapshot_slug} not found")

                # Validate split-based access restrictions
                if db_snapshot.split == Split.TEST:
                    raise ToolError(
                        f"Access denied: 'test' split is completely off-limits. "
                        f"You can only run evaluations on 'train' and 'valid' splits. "
                        f"Snapshot {snapshot_slug} is in 'test' split."
                    )

                # Look up example from database to validate it exists
                example = Example.from_spec_or_none(session, payload.example)

                if not example:
                    # List available examples for this snapshot
                    available = session.query(Example).filter_by(snapshot_slug=snapshot_slug).all()
                    example_list = "\n".join(
                        f"  - kind={ex.example_kind.value}, files_hash={ex.files_hash}" for ex in available[:10]
                    )
                    if len(available) > 10:
                        example_list += f"\n  ... and {len(available) - 10} more"

                    raise ToolError(
                        f"No example found matching {payload.example.model_dump()} "
                        f"in snapshot {snapshot_slug}.\n"
                        f"Available examples ({len(available)} total):\n{example_list}\n\n"
                        f"Query the examples table to find valid examples:\n"
                        f"SELECT snapshot_slug, example_kind, files_hash FROM examples WHERE snapshot_slug='{snapshot_slug}';"
                    )

                # Check if this is a per-file example (SingleFileSetExample) or whole-snapshot (WholeSnapshotExample)
                is_per_file = isinstance(payload.example, SingleFileSetExample)

                # Check VALID scope restrictions based on target metric mode
                if db_snapshot.split == Split.VALID and is_per_file and self._target_metric == TargetMetric.WHOLE_REPO:
                    # Access files_hash only for SingleFileSetExample (type narrowing)
                    assert isinstance(payload.example, SingleFileSetExample)
                    raise ToolError(
                        f"valid split in whole-repo mode requires whole-snapshot examples only. "
                        f"You requested a file_set example (files_hash={payload.example.files_hash}). "
                        f"Query for whole-snapshot examples: "
                        f"SELECT snapshot_slug, example_kind, files_hash FROM examples "
                        f"WHERE snapshot_slug='{snapshot_slug}' AND example_kind='whole_snapshot';"
                    )

            # Execute critic run using registry
            try:
                critic_run_id = await self._registry.run_critic(
                    image_ref=payload.definition_id,  # definition_id is actually an image ref
                    example=payload.example,
                    model=self._critic_model,
                    timeout_seconds=payload.timeout_seconds,
                    parent_run_id=self._optimizer_run_id,
                    budget_usd=payload.budget_usd,
                )
            except CriticExecutionError as e:
                raise ToolError(
                    f"Critic agent failed during execution: {e}\n\n"
                    f"{_trace_advice_for_snapshot(SnapshotSlug(snapshot_slug))}"
                ) from e
            except AgentDidNotSubmitError as e:
                raise ToolError(f"{e}\n\n{_AGENT_STUCK_ADVICE}\n{_trace_advice_for_run(e.agent_run_id)}") from e

            # Check status to provide specific error messages
            with get_session() as session:
                critic_run = session.get(AgentRun, critic_run_id)
                assert critic_run is not None
                status = critic_run.status

            if status == AgentRunStatus.MAX_TURNS_EXCEEDED:
                raise ToolError(
                    f"Critic agent exceeded maximum turns.\n\n"
                    f"{_AGENT_STUCK_ADVICE}\n"
                    f"{_trace_advice_for_run(critic_run_id)}"
                )
            if status == AgentRunStatus.CONTEXT_LENGTH_EXCEEDED:
                raise ToolError(
                    f"Critic agent exceeded context length.\n\n"
                    f"{_AGENT_STUCK_ADVICE}\n"
                    f"{_trace_advice_for_run(critic_run_id)}"
                )

            # At this point status must be COMPLETED
            return RunCriticOutput(critic_run_id=critic_run_id)

        self.run_critic_tool = self.flat_model()(run_critic)

        async def wait_until_graded(payload: WaitUntilGradedInput) -> WaitUntilGradedOutput:
            """Wait for a critic run to be fully graded (no remaining drift).

            Polls the grading_pending view until there are no remaining edges
            for the critic run, then returns the grading results.

            A critique is "graded" when all (issue, GT_occurrence) pairs have
            corresponding grading edges - not just when a grader run exists.
            This properly handles multiple grader daemons contributing edges.
            """
            start_time = time.monotonic()
            deadline = start_time + payload.timeout_seconds
            last_pending_count: int | None = None

            while time.monotonic() < deadline:
                with get_session() as session:
                    # Check for remaining drift using grading_pending view
                    pending_count = (
                        session.query(func.count())
                        .select_from(GradingPending)
                        .filter(GradingPending.critique_run_id == payload.critic_run_id)
                        .scalar()
                        or 0
                    )

                    if pending_count == 0:
                        # No drift - critique is fully graded
                        # Get the critic run to determine split and example type
                        critic_run = session.get(AgentRun, payload.critic_run_id)
                        if not critic_run:
                            raise ToolError(f"Critic run {payload.critic_run_id} not found")

                        critic_config = critic_run.critic_config()
                        example_spec = critic_config.example
                        snapshot_slug = example_spec.snapshot_slug
                        snapshot = session.query(Snapshot).filter_by(slug=snapshot_slug).one()
                        split = snapshot.split

                        # Find matching example to check scope kind
                        example = Example.from_spec(session, example_spec)
                        scope_kind = example.example_kind

                        # Compute grading metrics from edges for this critique
                        total_credit = (
                            session.query(func.sum(GradingEdge.credit))
                            .filter(GradingEdge.critique_run_id == payload.critic_run_id)
                            .filter(GradingEdge.tp_id.isnot(None))
                            .scalar()
                            or 0.0
                        )

                        max_credit = (
                            session.query(GradingEdge.tp_id, GradingEdge.tp_occurrence_id)
                            .filter(GradingEdge.critique_run_id == payload.critic_run_id)
                            .filter(GradingEdge.tp_id.isnot(None))
                            .distinct()
                            .count()
                        )

                        # Find the grader run(s) that contributed edges
                        grader_run_ids = (
                            session.query(GradingEdge.grader_run_id)
                            .filter(GradingEdge.critique_run_id == payload.critic_run_id)
                            .distinct()
                            .all()
                        )
                        # Use the first grader run ID for the response (usually there's only one)
                        grader_run_id = grader_run_ids[0][0] if grader_run_ids else payload.critic_run_id

                        # Build query advice
                        if (
                            split == Split.VALID
                            and scope_kind == ExampleKind.WHOLE_SNAPSHOT
                            and self._target_metric == TargetMetric.WHOLE_REPO
                        ):
                            query_advice = (
                                f"{_FUNCTION_BASED_METRICS_ADVICE} "
                                f"Example: SELECT * FROM {_VALIDATION_FUNCTION_NAME} WHERE critique_run_id = '{payload.critic_run_id}';"
                            )
                        elif (
                            split == Split.VALID
                            and scope_kind == ExampleKind.WHOLE_SNAPSHOT
                            and self._target_metric == TargetMetric.TARGETED
                        ):
                            query_advice = (
                                f"{_VIEW_BASED_METRICS_ADVICE} "
                                "IMPORTANT: Check n_examples >= 5 before trusting metrics. "
                                f"Use UCB/LCB bounds to quantify uncertainty."
                            )
                        else:
                            query_advice = (
                                f"{_VIEW_BASED_METRICS_ADVICE} "
                                "Example: SELECT recall_stats FROM recall_by_definition_split_kind WHERE critic_image_digest ='...';"
                            )

                        return WaitUntilGradedOutput(
                            grader_run_id=grader_run_id,
                            total_credit=float(total_credit),
                            max_credit=max_credit,
                            message=query_advice,
                        )

                    # Log progress if pending count changed
                    if last_pending_count != pending_count:
                        logger.debug(f"Waiting for grading: {pending_count} edges pending for {payload.critic_run_id}")
                        last_pending_count = pending_count

                # Not ready yet - wait before polling again
                await asyncio.sleep(payload.poll_interval_seconds)

            # Timeout reached
            raise ToolError(
                f"Timeout waiting for critic run {payload.critic_run_id} to be graded. "
                f"Waited {payload.timeout_seconds} seconds, {last_pending_count} edges still pending. "
                "Check if the grader daemon is running and processing critic runs."
            )

        self.wait_until_graded_tool = self.flat_model()(wait_until_graded)

        async def report_failure(payload: ReportFailureInput) -> str:
            """Report that optimization could not be completed.

            Use this when you determine the optimization run should be aborted
            (e.g., critical errors, no viable path forward).

            The agent loop will be stopped after this tool returns.
            """
            self._optimizer_state.error = payload.message
            return f"Optimization run marked as unsuccessful: {payload.message}"

        self.report_failure_tool = self.flat_model()(report_failure)
