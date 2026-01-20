"""Prompt optimizer: LLM agent for optimizing critic prompts via eval tools.

New architecture (in-container agent loop):
- Agent loop runs inside the container via CMD entrypoint
- Host serves PromptEvalServer via HTTP for orchestration tools (run_critic, run_grader)
- Container connects to host MCP server via MCP_SERVER_URL/MCP_SERVER_TOKEN
- Container exits 0 on success (submit), non-zero on failure (report_failure)
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import aiodocker
from fastmcp.exceptions import ToolError
from pydantic import Field
from sqlalchemy import func

from agent_core.turn_limit import MaxTurnsExceededError
from mcp_infra.enhanced.server import EnhancedFastMCP
from mcp_infra.flat_tool import FlatTool
from openai_utils.model import OpenAIModelProto
from openai_utils.pydantic_strict_mode import OpenAIStrictModeBaseModel
from props.core.agent_registry import AgentRegistry
from props.core.agent_types import AgentType, PromptOptimizerTypeConfig
from props.core.critic.exceptions import CriticExecutionError
from props.core.db.agent_definition_ids import PROMPT_OPTIMIZER_IMAGE_REF
from props.core.db.config import DatabaseConfig
from props.core.db.examples import Example
from props.core.db.models import AgentDefinition, AgentRun, AgentRunStatus, GradingEdge, Snapshot
from props.core.db.session import get_session
from props.core.display import short_uuid
from props.core.exceptions import AgentDidNotSubmitError
from props.core.ids import DefinitionId, SnapshotSlug
from props.core.loop_agent_env import run_loop_agent
from props.core.mcp_http_server import serve_mcp_http
from props.core.models.examples import ExampleKind, ExampleSpec, SingleFileSetExample
from props.core.oci_utils import BUILTIN_TAG, build_oci_reference, resolve_image_ref
from props.core.splits import Split

from .target_metric import TargetMetric

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
        description="agent_run_id of the critic agent run. Query agent_runs for output, costs, model. Pass to run_grader to grade against ground truth."
    )
    # cumulative_cost_usd: float = Field(
    #     description="Total cumulative cost (USD) for all critic/grader runs in this optimization session so far."
    # )


class RunGraderInput(OpenAIStrictModeBaseModel):
    critic_run_id: UUID = Field(description="agent_run_id of the critic agent run to grade (from run_critic output)")
    timeout_seconds: int = Field(description="Max seconds before container is killed")
    budget_usd: float | None = Field(description="Max USD cost for this agent (enforced by proxy)")


class RunGraderOutput(OpenAIStrictModeBaseModel):
    grader_run_id: UUID = Field(description="agent_run_id of the grader agent run. Run has been saved to database.")
    message: str = Field(
        description="Instructions for querying recall metrics from database views (aggregated across runs)."
    )
    # cumulative_cost_usd: float = Field(
    #     description="Total cumulative cost (USD) for all critic/grader runs in this optimization session so far."
    # )


class WaitUntilGradedInput(OpenAIStrictModeBaseModel):
    critic_run_id: UUID = Field(description="agent_run_id of the critic run to wait for grading")
    timeout_seconds: int = Field(
        default=300,
        ge=10,
        le=3600,
        description="Maximum time to wait for grading (default 300s, max 1 hour)",
    )
    poll_interval_seconds: int = Field(
        default=5,
        ge=1,
        le=60,
        description="How often to check for grading completion (default 5s)",
    )


class WaitUntilGradedOutput(OpenAIStrictModeBaseModel):
    grader_run_id: UUID = Field(description="agent_run_id of the grader run that graded this critic")
    total_credit: float = Field(description="Sum of credits for TP matches (recall numerator)")
    max_credit: int = Field(description="Number of distinct TP occurrences (recall denominator)")
    message: str = Field(description="Query advice for getting aggregate metrics")


class PromptEvalServer(EnhancedFastMCP):
    RUN_CRITIC_TOOL = "run_critic"
    RUN_GRADER_TOOL = "run_grader"
    WAIT_UNTIL_GRADED_TOOL = "wait_until_graded"

    run_critic_tool: FlatTool[Any, Any]
    run_grader_tool: FlatTool[Any, Any]
    wait_until_graded_tool: FlatTool[Any, Any]
    report_failure_tool: FlatTool[Any, Any]

    def __init__(
        self,
        *,
        critic_client: OpenAIModelProto,
        grader_client: OpenAIModelProto,
        registry: AgentRegistry,
        optimizer_state: PromptOptimizerState,
        target_metric: TargetMetric,
        optimizer_run_id: UUID,
    ):
        super().__init__(
            "prompt_eval",
            instructions=(
                "Agent definition evaluation tools: "
                "run_critic(definition_id, example) - run critic agent on example, "
                "run_grader(critic_run_id) - manually run grader (deprecated, prefer wait_until_graded), "
                "wait_until_graded(critic_run_id) - wait for grader daemon to grade critic run. "
                "Create packages via CLI: props agent-pkg create /workspace/my_critic/. "
                "Query the database for results, costs, and metrics. "
                "Use report_failure to declare the run unsuccessful and abort."
            ),
        )

        # Store parameters for use in tools
        self._critic_client = critic_client
        self._grader_client = grader_client
        self._registry = registry
        self._optimizer_state = optimizer_state
        self._target_metric = target_metric
        self._optimizer_run_id = optimizer_run_id

        # Note: Agent run ID is available via current_agent_run_id() SQL function
        # which extracts it from the database username pattern (agent_{uuid}).

        async def run_critic(payload: RunCriticInput) -> RunCriticOutput:
            """Run critic agent using an agent package.

            Loads critic package from database and runs the /init script to get
            the system prompt, then runs the critic on the specified example.

            Validates split-based access restrictions:
            - TRAIN split: all example types allowed
            - VALID split: restrictions depend on target_metric mode
            - TEST split: completely off-limits

            Returns critic_run_id for subsequent grading with run_grader.
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
                    model=self._critic_client.model,
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

        async def run_grader(payload: RunGraderInput) -> RunGraderOutput:
            """Run grader agent to evaluate a critique against ground truth.

            Saves grader run to database with per-occurrence credits.

            To get recall metrics, query aggregate views (see docs/db/evaluation_flow.md):
            - recall_by_definition_split_kind: Recall per (agent_definition_id, models, split, example_kind)
            - recall_by_example: Recall per (example, models)

            Returns grader_run_id and instructions for querying metrics.
            """
            # Execute GraderRun by critic_run_id (fetches critic run from DB, saves grader run to DB)
            try:
                grader_run_id = await self._registry.run_grader(
                    critic_run_id=payload.critic_run_id,
                    model=self._grader_client.model,
                    timeout_seconds=payload.timeout_seconds,
                    parent_run_id=self._optimizer_run_id,
                    budget_usd=payload.budget_usd,
                )
            except AgentDidNotSubmitError as e:
                raise ToolError(
                    f"{e}\n\n{_AGENT_STUCK_ADVICE}\n{_trace_advice_for_run(e.agent_run_id, is_grader=True)}"
                ) from e
            except MaxTurnsExceededError as e:
                raise ToolError(f"Grader agent exceeded maximum turns: {e}\n\n{_AGENT_STUCK_ADVICE}") from e

            # Verify grader run succeeded
            # Note: grader_run_id is always UUID here - the except block always raises
            with get_session() as session:
                grader_run = session.get(AgentRun, grader_run_id)
                if not grader_run:
                    raise ToolError(f"Grader run {grader_run_id} not found in database")
                if grader_run.status != AgentRunStatus.COMPLETED:
                    raise ToolError(
                        f"Grader run {grader_run_id} did not complete successfully (status={grader_run.status.value})\n\n"
                        f"{_AGENT_STUCK_ADVICE}\n"
                        f"{_trace_advice_for_run(grader_run_id, is_grader=True)}"
                    )

                # Determine split and whether this is a full-snapshot run
                # Get example spec from the graded critic run
                graded_critic_run_id = grader_run.grader_config().graded_agent_run_id
                critic_run = session.get(AgentRun, graded_critic_run_id)
                if not critic_run:
                    raise ToolError(f"Grader run {grader_run_id} has no associated critic run")
                critic_config = critic_run.critic_config()
                example_spec = critic_config.example
                snapshot_slug = example_spec.snapshot_slug
                snapshot = session.query(Snapshot).filter_by(slug=snapshot_slug).one()
                split = snapshot.split

                # Find matching example to check scope kind
                example = Example.from_spec(session, example_spec)  # Raises if not found - data integrity error

                # Get example kind from the example itself
                scope_kind = example.example_kind

                # Compute immediate feedback from this grader run (direct query to grading_edges)
                # Pattern 1: Total credit (recall numerator)
                total_credit = (
                    session.query(func.sum(GradingEdge.credit))
                    .filter_by(grader_run_id=grader_run_id)
                    .filter(GradingEdge.tp_id.isnot(None))  # Only TP matches
                    .scalar()
                    or 0.0
                )

                # Pattern 2: Occurrence count (recall denominator)
                max_credit = (
                    session.query(GradingEdge.tp_id, GradingEdge.tp_occurrence_id)
                    .filter_by(grader_run_id=grader_run_id)
                    .filter(GradingEdge.tp_id.isnot(None))
                    .distinct()
                    .count()
                )

                # Build message with immediate feedback and query advice
                immediate_feedback = (
                    f"Grader run {grader_run_id} completed successfully. "
                    f"Total credit: {total_credit:.2f} of {max_credit}. "
                )

                # Add query advice based on split, example type, and optimization mode
                if (
                    split == Split.VALID
                    and scope_kind == ExampleKind.WHOLE_SNAPSHOT
                    and self._target_metric == TargetMetric.WHOLE_REPO
                ):
                    # VALID full-snapshot in whole-repo mode: use validation function
                    query_advice = (
                        f"{_FUNCTION_BASED_METRICS_ADVICE} "
                        f"Example: SELECT * FROM {_VALIDATION_FUNCTION_NAME} WHERE grader_run_id = '{grader_run_id}'; "
                        f"For full details: SELECT * FROM agent_runs WHERE agent_run_id = '{grader_run_id}';"
                    )
                elif (
                    split == Split.VALID
                    and scope_kind == ExampleKind.WHOLE_SNAPSHOT
                    and self._target_metric == TargetMetric.TARGETED
                ):
                    # VALID full-snapshot in targeted mode: use aggregate views
                    query_advice = (
                        f"{_VIEW_BASED_METRICS_ADVICE} "
                        "IMPORTANT: Check n_examples >= 5 before trusting metrics (small samples have high variance). "
                        "Use UCB/LCB bounds to quantify uncertainty. "
                        f"Example: SELECT recall_stats, n_examples FROM recall_by_definition_split_kind "
                        f"WHERE critic_image_digest ='...' AND split='valid' AND example_kind='{ExampleKind.WHOLE_SNAPSHOT}'; "
                        f"For full details: SELECT * FROM agent_runs WHERE agent_run_id = '{grader_run_id}';"
                    )
                else:
                    # TRAIN split or per-file examples: use aggregate views
                    query_advice = (
                        f"{_VIEW_BASED_METRICS_ADVICE} "
                        "Example: SELECT recall_stats FROM recall_by_definition_split_kind WHERE critic_image_digest ='...' AND split='train'; "
                        f"For full details: SELECT * FROM agent_runs WHERE agent_run_id = '{grader_run_id}';"
                    )

                message = immediate_feedback + query_advice

            return RunGraderOutput(grader_run_id=grader_run_id, message=message)

        self.run_grader_tool = self.flat_model()(run_grader)

        async def wait_until_graded(payload: WaitUntilGradedInput) -> WaitUntilGradedOutput:
            """Wait for a critic run to be graded by the grader daemon.

            Polls the database until a grader run exists that graded the specified
            critic run, then returns the grading results.

            This is the preferred way to get grading results when a grader daemon
            is running. The daemon automatically grades completed critic runs.
            """
            import time

            start_time = time.monotonic()
            deadline = start_time + payload.timeout_seconds

            while time.monotonic() < deadline:
                # Look for a grader run that graded this critic
                with get_session() as session:
                    # Query for grader runs where type_config contains the critic_run_id
                    # GraderTypeConfig stores graded_agent_run_id
                    grader_runs = (
                        session.query(AgentRun)
                        .filter(
                            AgentRun.type_config["graded_agent_run_id"].astext == str(payload.critic_run_id),
                            AgentRun.status == AgentRunStatus.COMPLETED,
                        )
                        .all()
                    )

                    if grader_runs:
                        # Found a completed grader run - extract results
                        grader_run = grader_runs[0]  # Take the first (usually only one)
                        grader_run_id = grader_run.agent_run_id

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

                        # Compute grading metrics
                        total_credit = (
                            session.query(func.sum(GradingEdge.credit))
                            .filter_by(grader_run_id=grader_run_id)
                            .filter(GradingEdge.tp_id.isnot(None))
                            .scalar()
                            or 0.0
                        )

                        max_credit = (
                            session.query(GradingEdge.tp_id, GradingEdge.tp_occurrence_id)
                            .filter_by(grader_run_id=grader_run_id)
                            .filter(GradingEdge.tp_id.isnot(None))
                            .distinct()
                            .count()
                        )

                        # Build query advice
                        if (
                            split == Split.VALID
                            and scope_kind == ExampleKind.WHOLE_SNAPSHOT
                            and self._target_metric == TargetMetric.WHOLE_REPO
                        ):
                            query_advice = (
                                f"{_FUNCTION_BASED_METRICS_ADVICE} "
                                f"Example: SELECT * FROM {_VALIDATION_FUNCTION_NAME} WHERE grader_run_id = '{grader_run_id}';"
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

                # Not ready yet - wait before polling again
                await asyncio.sleep(payload.poll_interval_seconds)

            # Timeout reached
            raise ToolError(
                f"Timeout waiting for critic run {payload.critic_run_id} to be graded. "
                f"Waited {payload.timeout_seconds} seconds. "
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


# Default LLM proxy URL (same as agent_registry default)
DEFAULT_LLM_PROXY_URL = "http://props-llm-proxy:5052"


async def run_prompt_optimizer(
    budget: float,
    optimizer_client: OpenAIModelProto,
    critic_client: OpenAIModelProto,
    grader_client: OpenAIModelProto,
    docker_client: aiodocker.Docker,
    target_metric: TargetMetric,
    db_config: DatabaseConfig,
    timeout_seconds: int,
    image_ref: str = BUILTIN_TAG,
    llm_proxy_url: str = DEFAULT_LLM_PROXY_URL,
) -> None:
    """Run prompt optimizer agent with in-container agent loop.

    New architecture:
    - Agent loop runs inside the container (not on host)
    - Host serves PromptEvalServer via HTTP for orchestration tools
    - Container connects to host MCP server via MCP_SERVER_URL/MCP_SERVER_TOKEN
    - Container exits 0 on success, non-zero on failure
    """
    # Get train snapshots from database
    with get_session() as session:
        train_snapshots = session.query(Snapshot).filter_by(split=Split.TRAIN).all()
        train_slugs = [SnapshotSlug(s.slug) for s in train_snapshots]

    logger.info(f"Using {len(train_slugs)} train snapshots (agent will fetch from database)")

    # Generate unique ID for this run
    agent_run_id = uuid4()
    logger.info(f"Prompt optimizer agent_run_id: {agent_run_id}")

    # Resolve image reference to digest and construct full OCI reference
    image_digest = resolve_image_ref(AgentType.PROMPT_OPTIMIZER, image_ref)
    image = build_oci_reference(AgentType.PROMPT_OPTIMIZER, image_digest)
    logger.info(f"Resolved prompt-optimizer image {image_ref} → {image}")

    # Phase 1: Write initial AgentRun to DB (BEFORE agent runs - FK constraint!)
    with get_session() as session:
        type_config = PromptOptimizerTypeConfig(
            target_metric=target_metric,
            optimizer_model=optimizer_client.model,
            critic_model=critic_client.model,
            grader_model=grader_client.model,
            budget_limit=budget,
        )

        agent_run = AgentRun(
            agent_run_id=agent_run_id,
            image_digest=image_digest,
            model=optimizer_client.model,
            type_config=type_config,
            status=AgentRunStatus.IN_PROGRESS,
        )
        session.add(agent_run)
        session.commit()

    logger.info(f"Created prompt optimizer AgentRun: {agent_run_id}")

    # Create registry for critic/grader runs initiated by the optimizer
    registry = AgentRegistry(
        docker_client=docker_client,
        db_config=db_config,
        llm_proxy_url=llm_proxy_url,
    )

    # Create PromptEvalServer with orchestration tools
    optimizer_state = PromptOptimizerState()
    prompt_eval_server = PromptEvalServer(
        critic_client=critic_client,
        grader_client=grader_client,
        registry=registry,
        optimizer_state=optimizer_state,
        target_metric=target_metric,
        optimizer_run_id=agent_run_id,
    )

    try:
        # Serve PromptEvalServer via HTTP for container to connect
        with serve_mcp_http(prompt_eval_server) as mcp_handle:
            logger.info(f"MCP server available at {mcp_handle.url}")

            # Run the container with in-container agent loop
            result = await run_loop_agent(
                docker_client=docker_client,
                agent_run_id=agent_run_id,
                db_config=db_config,
                image=image,
                llm_proxy_url=llm_proxy_url,
                extra_env={
                    "MCP_SERVER_URL": mcp_handle.url,
                    "MCP_SERVER_TOKEN": mcp_handle.token,
                },
                container_name=f"promptopt-{short_uuid(agent_run_id)}",
                timeout_seconds=timeout_seconds,
            )

            timed_out = result.exit_code == -1
            if timed_out:
                logger.error(f"Container timed out after {timeout_seconds} seconds")
            else:
                logger.info(f"Container exited with code {result.exit_code}")
            if result.stderr:
                logger.info(f"Container stderr:\n{result.stderr}")

        # Update status based on exit code
        if timed_out:
            final_status = AgentRunStatus.TIMED_OUT
        elif result.exit_code == 0:
            final_status = AgentRunStatus.COMPLETED
        else:
            final_status = AgentRunStatus.REPORTED_FAILURE
        with get_session() as session:
            agent_run = session.get(AgentRun, agent_run_id)
            if agent_run:
                agent_run.status = final_status
                session.commit()
                logger.info(f"Updated agent_run status to {final_status.value}")

    finally:
        # Clean up registry resources
        await registry.close()

    logger.info("Optimization session complete.")
    logger.info(f"Budget: ${budget:.2f}")
