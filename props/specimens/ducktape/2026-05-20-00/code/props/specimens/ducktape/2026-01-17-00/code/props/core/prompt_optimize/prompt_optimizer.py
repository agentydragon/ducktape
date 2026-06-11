"""Prompt optimizer: LLM agent for optimizing critic prompts via eval tools."""

import logging
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import aiodocker
from fastmcp.client import Client
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AuthProvider
from fastmcp.tools import FunctionTool
from pydantic import Field
from sqlalchemy import func

from agent_core.handler import AbortIf, RedirectOnTextMessageHandler
from agent_core.turn_limit import MaxTurnsExceededError
from mcp_infra.display.rich_display import CompactDisplayHandler
from mcp_infra.enhanced.server import EnhancedFastMCP
from openai_utils.model import OpenAIModelProto, UserMessage
from openai_utils.pydantic_strict_mode import OpenAIStrictModeBaseModel
from openai_utils.types import ReasoningSummary
from props.core.agent_handle import AgentHandle
from props.core.agent_registry import AgentRegistry
from props.core.agent_setup import AgentEnvironment
from props.core.agent_types import AgentType, PromptOptimizerTypeConfig
from props.core.agent_workspace import WorkspaceManager
from props.core.cli.common_options import DEFAULT_MAX_LINES
from props.core.critic.exceptions import CriticExecutionError
from props.core.db.agent_definition_ids import PROMPT_OPTIMIZER_IMAGE_REF
from props.core.db.config import DatabaseConfig
from props.core.db.examples import Example
from props.core.db.models import AgentDefinition, AgentRun, AgentRunStatus, GradingEdge, Snapshot
from props.core.db.session import get_session
from props.core.display import short_uuid
from props.core.exceptions import AgentDidNotSubmitError
from props.core.ids import DefinitionId, SnapshotSlug
from props.core.models.examples import ExampleKind, ExampleSpec, SingleFileSetExample
from props.core.oci_utils import BUILTIN_TAG, build_oci_reference, resolve_image_ref
from props.core.prompt_optimize.budget_handler import BudgetEnforcementHandler
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


class PromptOptimizerAgentEnvironment(AgentEnvironment):
    prompt_eval_server: EnhancedFastMCP
    optimizer_state: PromptOptimizerState

    def __init__(
        self,
        docker_client: aiodocker.Docker,
        optimizer_run_id: UUID,
        optimizer_model: str,
        critic_client: OpenAIModelProto,
        grader_client: OpenAIModelProto,
        db_config: DatabaseConfig,
        optimizer_state: PromptOptimizerState,
        target_metric: TargetMetric,
        budget_limit: float,
        workspace_manager: WorkspaceManager,
        registry: AgentRegistry,
        verbose: bool = False,
        *,
        image: str,
    ):
        self._optimizer_run_id = optimizer_run_id
        self._optimizer_model = optimizer_model
        self._critic_client = critic_client
        self._grader_client = grader_client
        self._db_config = db_config
        self.optimizer_state = optimizer_state  # Exposed for abort checking
        self._target_metric = target_metric
        self._budget_limit = budget_limit
        self._registry = registry
        self._verbose = verbose

        super().__init__(
            agent_run_id=optimizer_run_id,
            docker_client=docker_client,
            db_config=db_config,
            workspace_manager=workspace_manager,
            image=image,
            container_name=f"promptopt-{short_uuid(optimizer_run_id)}",
            labels={
                "adgn.project": "props",
                "adgn.role": "prompt_optimize",
                "adgn.agent_run_id": str(optimizer_run_id),
            },
            auto_remove=True,
        )

    @property
    def type_config(self) -> PromptOptimizerTypeConfig:
        return PromptOptimizerTypeConfig(
            target_metric=self._target_metric,
            optimizer_model=self._optimizer_model,
            critic_model=self._critic_client.model,
            grader_model=self._grader_client.model,
            budget_limit=self._budget_limit,
        )

    def _make_mcp_server(self, auth: AuthProvider) -> EnhancedFastMCP:
        server = PromptEvalServer(
            critic_client=self._critic_client,
            grader_client=self._grader_client,
            registry=self._registry,
            optimizer_state=self.optimizer_state,
            target_metric=self._target_metric,
            optimizer_run_id=self._optimizer_run_id,
            workspace_root=self.workspace_root,
            budget_limit=self._budget_limit,
            verbose=self._verbose,
        )
        self.prompt_eval_server = server
        return server


class RunCriticInput(OpenAIStrictModeBaseModel):
    definition_id: DefinitionId = Field(
        description="Agent package ID (from 'props agent-pkg create' or 'critic' for baseline)"
    )
    example: ExampleSpec = Field(description="Example to evaluate (WholeSnapshotExample or SingleFileSetExample)")
    max_turns: int = Field(ge=200, le=200, description="Maximum sampling turns (fixed at 200)")


class RunCriticOutput(OpenAIStrictModeBaseModel):
    critic_run_id: UUID = Field(
        description="agent_run_id of the critic agent run. Query agent_runs for output, costs, model. Pass to run_grader to grade against ground truth."
    )
    # cumulative_cost_usd: float = Field(
    #     description="Total cumulative cost (USD) for all critic/grader runs in this optimization session so far."
    # )


class RunGraderInput(OpenAIStrictModeBaseModel):
    critic_run_id: UUID = Field(description="agent_run_id of the critic agent run to grade (from run_critic output)")
    max_turns: int = Field(ge=200, le=200, description="Maximum sampling turns (fixed at 200)")


class RunGraderOutput(OpenAIStrictModeBaseModel):
    grader_run_id: UUID = Field(description="agent_run_id of the grader agent run. Run has been saved to database.")
    message: str = Field(
        description="Instructions for querying recall metrics from database views (aggregated across runs)."
    )
    # cumulative_cost_usd: float = Field(
    #     description="Total cumulative cost (USD) for all critic/grader runs in this optimization session so far."
    # )


class PromptEvalServer(EnhancedFastMCP):
    RUN_CRITIC_TOOL = "run_critic"
    RUN_GRADER_TOOL = "run_grader"

    run_critic_tool: FunctionTool
    run_grader_tool: FunctionTool
    report_failure_tool: FunctionTool

    def __init__(
        self,
        *,
        critic_client: OpenAIModelProto,
        grader_client: OpenAIModelProto,
        registry: AgentRegistry,
        optimizer_state: PromptOptimizerState,
        target_metric: TargetMetric,
        optimizer_run_id: UUID,
        workspace_root: Path,
        budget_limit: float,
        verbose: bool = False,
    ):
        super().__init__(
            "prompt_eval",
            instructions=(
                "Agent definition evaluation tools: "
                "run_critic(definition_id, example) - run critic agent on example, "
                "run_grader(critic_run_id) - grade critiques against ground truth. "
                "Create packages via CLI: props agent-pkg create /workspace/my_critic/."
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
        self._workspace_root = workspace_root
        self._budget_limit = budget_limit
        self._verbose = verbose

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
                    client=self._critic_client,
                    parent_run_id=self._optimizer_run_id,
                    verbose=self._verbose,
                    max_turns=payload.max_turns,
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
                    f"Critic agent exceeded maximum turns ({payload.max_turns}).\n\n"
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
                    client=self._grader_client,
                    parent_run_id=self._optimizer_run_id,
                    verbose=self._verbose,
                    max_turns=payload.max_turns,
                )
            except AgentDidNotSubmitError as e:
                raise ToolError(
                    f"{e}\n\n{_AGENT_STUCK_ADVICE}\n{_trace_advice_for_run(e.agent_run_id, is_grader=True)}"
                ) from e
            except MaxTurnsExceededError as e:
                raise ToolError(
                    f"Grader agent exceeded maximum turns ({payload.max_turns}): {e}\n\n{_AGENT_STUCK_ADVICE}"
                ) from e

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

        async def report_failure(payload: ReportFailureInput) -> str:
            """Report that optimization could not be completed.

            Use this when you determine the optimization run should be aborted
            (e.g., critical errors, no viable path forward).

            The agent loop will be stopped after this tool returns.
            """
            self._optimizer_state.error = payload.message
            return f"Optimization run marked as unsuccessful: {payload.message}"

        self.report_failure_tool = self.flat_model()(report_failure)


async def run_prompt_optimizer(
    budget: float,
    optimizer_client: OpenAIModelProto,
    critic_client: OpenAIModelProto,
    grader_client: OpenAIModelProto,
    docker_client: aiodocker.Docker,
    target_metric: TargetMetric,
    db_config: DatabaseConfig,
    verbose: bool = False,
    max_lines: int = DEFAULT_MAX_LINES,
    image_ref: str = BUILTIN_TAG,
) -> None:
    """Run prompt optimizer agent. Loops until budget exhausted or report_failure called."""
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

    # Create agent environment with prompt eval HTTP MCP server and temporary user
    # AgentEnvironment creates temporary database user with TRAIN-split-only access
    # AgentEnvironment handles HTTP server and container lifecycle (snapshots fetched by agents at init)
    workspace_manager = WorkspaceManager.from_env()

    # Create registry for critic/grader runs initiated by the optimizer
    registry = AgentRegistry(docker_client=docker_client, db_config=db_config, workspace_manager=workspace_manager)

    agent_env = PromptOptimizerAgentEnvironment(
        docker_client=docker_client,
        optimizer_run_id=agent_run_id,
        optimizer_model=optimizer_client.model,
        critic_client=critic_client,
        grader_client=grader_client,
        db_config=db_config,
        optimizer_state=PromptOptimizerState(),
        target_metric=target_metric,
        budget_limit=budget,
        workspace_manager=workspace_manager,
        registry=registry,
        verbose=verbose,
        image=image,
    )
    async with agent_env as comp:
        # comp is a PropertiesDockerCompositor with:
        # - comp.runtime (Docker exec server)
        # - HTTP MCP server with prompt_eval tools (accessed via MCP client)

        # TODO: Auto-infer prompt_optimization_run_id in MCP server tools instead of manually passing it here
        # The prompt eval server (and grader/critic tools) should be able to auto-detect when they're
        # being called within a PO session context (e.g., via environment variable, session metadata,
        # or resource lookup) rather than requiring manual ID propagation through all tool calls.
        # This would eliminate the need to manually set prompt_optimization_run_id in RunCriticInput
        # and RunGraderInput.

        user = f"""Your budget is: ${budget:.2f}.

Models in use:
- Optimizer (you): {optimizer_client.model}
- Critic: {critic_client.model}
- Grader: {grader_client.model}

Note: The database may contain results from other models. These historical results might provide useful insights for optimization.

Iterate to find an optimal prompt for a code reviewer/critic LLM agent.
Prioritize recall.
"""

        def _optimizer_should_abort() -> bool:
            """Check if optimizer reported failure."""
            return agent_env.optimizer_state.error is not None

        # Build handlers for prompt optimizer agent
        # NOTE: Do NOT call build_props_handlers() here - AgentHandle.create() already adds
        # DatabaseEventHandler. We only add CompactDisplayHandler if verbose is enabled.
        handlers: list = []
        if verbose:
            display_handler = await CompactDisplayHandler.from_compositor(
                comp, max_lines=max_lines, prefix=f"[OPTIMIZER:{short_uuid(agent_run_id)}] "
            )
            handlers.append(display_handler)

        handlers.extend(
            [
                RedirectOnTextMessageHandler(
                    reminder_message=(
                        "Text messages won't be delivered. Continue optimization work via MCP tools "
                        "(run_critic, run_grader). Report completion or failure via tools."
                    )
                ),
                AbortIf(should_abort=_optimizer_should_abort),
            ]
        )

        # Note: resources and compositor_meta are auto-mounted by base Compositor
        async with Client(comp) as mcp_client:
            # Create AgentHandle - reads system prompt from container via MCP, runs init
            handle = await AgentHandle.create(
                agent_run_id=agent_run_id,
                image_digest=PROMPT_OPTIMIZER_IMAGE_REF,
                model_client=optimizer_client,
                mcp_client=mcp_client,
                compositor=comp,
                handlers=handlers,
                parallel_tool_calls=True,
                reasoning_summary=ReasoningSummary.DETAILED,
            )

            # Add budget enforcement handler after agent creation (needs agent reference)
            budget_handler = BudgetEnforcementHandler(
                optimizer_run_id=agent_run_id, budget_limit=budget, agent=handle.agent
            )
            handle.agent._handlers.append(budget_handler)

            handle.process_message(UserMessage.text(user))
            logger.debug("Starting agent.run()")
            await handle.run()
            logger.debug("Agent run complete")
    # Compositor.__aexit__ unmounts all non-pinned servers and cleans up containers here

    # Clean up registry resources
    await registry.close()

    logger.info("Optimization session complete.")
    logger.info(f"Budget: ${budget:.2f}")
