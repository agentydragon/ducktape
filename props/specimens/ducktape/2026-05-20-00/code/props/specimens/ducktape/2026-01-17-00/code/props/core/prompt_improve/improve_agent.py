"""Improvement agent: creates definitions to beat baseline on allowed examples."""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid4

import aiodocker
from fastmcp.client import Client
from fastmcp.server.auth import AuthProvider
from pydantic import BaseModel, Field

from agent_core.handler import AbortIf
from agent_core.turn_limit import MaxTurnsHandler
from mcp_infra.display.rich_display import CompactDisplayHandler
from mcp_infra.enhanced.server import EnhancedFastMCP
from openai_utils.model import OpenAIModelProto
from openai_utils.types import ReasoningSummary
from props.core.agent_handle import AgentHandle
from props.core.agent_registry import AgentRegistry
from props.core.agent_setup import AgentEnvironment
from props.core.agent_types import AgentType, ImprovementTypeConfig
from props.core.agent_workspace import WorkspaceManager
from props.core.cli.common_options import DEFAULT_MAX_LINES
from props.core.db.agent_definition_ids import IMPROVEMENT_IMAGE_REF
from props.core.db.config import DatabaseConfig
from props.core.db.models import AgentRun, AgentRunStatus
from props.core.db.session import get_session
from props.core.display import short_uuid
from props.core.models.examples import ExampleSpec
from props.core.oci_utils import BUILTIN_TAG, build_oci_reference, resolve_image_ref
from props.core.prompt_improve.reminder_handler import ImprovementReminderHandler, TerminationSuccess
from props.core.prompt_improve.token_budget_handler import TokenBudgetHandler
from props.core.prompt_optimize.prompt_optimizer import PromptEvalServer, PromptOptimizerState
from props.core.prompt_optimize.target_metric import TargetMetric

logger = logging.getLogger(__name__)


class OutcomeExhausted(BaseModel):
    kind: Literal["exhausted"] = "exhausted"


class OutcomeUnexpectedTermination(BaseModel):
    kind: Literal["unexpected_termination"] = "unexpected_termination"
    message: str


ImprovementOutcome = Annotated[
    TerminationSuccess | OutcomeExhausted | OutcomeUnexpectedTermination, Field(discriminator="kind")
]


class ImprovementResult(BaseModel):
    tokens_used: int
    run_id: UUID
    outcome: ImprovementOutcome


class ImprovementAgentEnvironment(AgentEnvironment):
    prompt_eval_server: PromptEvalServer
    agent_state: PromptOptimizerState

    def __init__(
        self,
        docker_client: aiodocker.Docker,
        improvement_run_id: UUID,
        baseline_image_refs: list[str],
        allowed_examples: list[ExampleSpec],
        improvement_model: str,
        critic_client: OpenAIModelProto,
        grader_client: OpenAIModelProto,
        db_config: DatabaseConfig,
        workspace_manager: WorkspaceManager,
        registry: AgentRegistry,
        verbose: bool = False,
        *,
        image: str,
    ):
        type_config = ImprovementTypeConfig(
            baseline_image_refs=baseline_image_refs,
            allowed_examples=allowed_examples,
            improvement_model=improvement_model,
            critic_model=critic_client.model,
            grader_model=grader_client.model,
        )
        self._type_config = type_config

        self._critic_client = critic_client
        self._grader_client = grader_client
        self._registry = registry
        self._verbose = verbose
        self.agent_state = PromptOptimizerState()

        super().__init__(
            agent_run_id=improvement_run_id,
            docker_client=docker_client,
            db_config=db_config,
            workspace_manager=workspace_manager,
            image=image,
            container_name=f"improve-{short_uuid(improvement_run_id)}",
            labels={"adgn.project": "props", "adgn.role": "improve", "adgn.agent_run_id": str(improvement_run_id)},
            auto_remove=True,
        )

    @property
    def type_config(self) -> ImprovementTypeConfig:
        return self._type_config

    def _make_mcp_server(self, auth: AuthProvider) -> EnhancedFastMCP:
        server = PromptEvalServer(
            critic_client=self._critic_client,
            grader_client=self._grader_client,
            registry=self._registry,
            optimizer_state=self.agent_state,
            target_metric=TargetMetric.TARGETED,
            optimizer_run_id=self._agent_run_id,
            workspace_root=self.workspace_root,
            budget_limit=float("inf"),
            verbose=self._verbose,
        )
        self.prompt_eval_server = server
        return server


async def run_improvement_agent(
    examples: list[ExampleSpec],
    baseline_image_refs: list[str],
    token_budget: int,
    model: str,
    docker_client: aiodocker.Docker,
    db_config: DatabaseConfig,
    client: OpenAIModelProto,
    critic_client: OpenAIModelProto,
    grader_client: OpenAIModelProto,
    output_dir: Path | None = None,
    verbose: bool = False,
) -> ImprovementResult:
    """Run improvement agent to optimize prompts.

    Always uses builtin improvement image for consistency."""
    if not examples:
        raise ValueError("examples must not be empty")

    run_id = uuid4()
    if output_dir is None:
        output_dir = Path(tempfile.mkdtemp(prefix=f"improve_agent_{str(run_id)[:8]}_"))

    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    logger.info(
        f"Starting improvement agent run {run_id}: "
        f"{len(examples)} examples, {token_budget:,} token budget, model={model}"
    )
    logger.info(f"Output directory: {output_dir}")

    # Always use builtin improvement image
    image_digest = resolve_image_ref(AgentType.IMPROVEMENT, BUILTIN_TAG)
    image = build_oci_reference(AgentType.IMPROVEMENT, image_digest)
    logger.info(f"Using builtin improvement image: {image_digest}")

    type_config = ImprovementTypeConfig(
        baseline_image_refs=baseline_image_refs,
        allowed_examples=examples,
        improvement_model=model,
        critic_model=critic_client.model,
        grader_model=grader_client.model,
    )

    with get_session() as session:
        agent_run = AgentRun(
            agent_run_id=run_id,
            image_digest=image_digest,
            model=model,
            type_config=type_config,
            status=AgentRunStatus.IN_PROGRESS,
        )
        session.add(agent_run)
        session.commit()

    workspace_manager = WorkspaceManager.from_env()
    registry = AgentRegistry(docker_client=docker_client, db_config=db_config, workspace_manager=workspace_manager)
    agent_env = ImprovementAgentEnvironment(
        docker_client=docker_client,
        improvement_run_id=run_id,
        baseline_image_refs=baseline_image_refs,
        allowed_examples=examples,
        improvement_model=model,
        critic_client=critic_client,
        grader_client=grader_client,
        db_config=db_config,
        workspace_manager=workspace_manager,
        registry=registry,
        verbose=verbose,
        image=image,
    )

    try:
        async with agent_env as comp:
            token_handler = TokenBudgetHandler(max_tokens=token_budget)
            reminder_handler = ImprovementReminderHandler(
                improvement_run_id=run_id, type_config=type_config, db_config=db_config
            )

            handlers: list = []
            if verbose:
                display_handler = await CompactDisplayHandler.from_compositor(
                    comp, max_lines=DEFAULT_MAX_LINES, prefix=f"[IMPROVE {str(run_id)[:8]}] "
                )
                handlers.append(display_handler)
            handlers.extend(
                [
                    reminder_handler,
                    AbortIf(should_abort=lambda: agent_env.agent_state.error is not None),
                    token_handler,
                    MaxTurnsHandler(max_turns=200),
                ]
            )

            async with Client(comp) as mcp_client:
                # Create AgentHandle - reads system prompt from container via MCP, runs init
                handle = await AgentHandle.create(
                    agent_run_id=run_id,
                    image_digest=IMPROVEMENT_IMAGE_REF,
                    model_client=client,
                    mcp_client=mcp_client,
                    compositor=comp,
                    handlers=handlers,
                    parallel_tool_calls=True,
                    reasoning_summary=ReasoningSummary.DETAILED,
                )

                logger.info("Starting agent loop")
                await handle.run()
                logger.info("Agent loop completed")

            tokens_used = token_handler.cumulative_tokens
            last_result = reminder_handler.last_result

            outcome: ImprovementOutcome
            if isinstance(last_result, TerminationSuccess):
                logger.info(
                    f"Improvement succeeded: definition '{last_result.definition_id}' "
                    f"with {last_result.total_credit:.1f} issues "
                    f"beats baseline avg {last_result.baseline_avg:.1f} (run_id={run_id})"
                )
                outcome = last_result
            elif token_handler.percentage_used >= 1.0:
                outcome = OutcomeExhausted()
            elif agent_env.agent_state.error is not None:
                outcome = OutcomeUnexpectedTermination(message=f"Agent reported failure: {agent_env.agent_state.error}")
            else:
                outcome = OutcomeUnexpectedTermination(
                    message=f"Agent terminated with {token_handler.percentage_used:.1%} "
                    f"budget used without beating baseline or exhaustion"
                )

            logger.info(f"Improvement agent completed: kind={outcome.kind}, tokens={tokens_used:,}/{token_budget:,}")
            return ImprovementResult(tokens_used=tokens_used, run_id=run_id, outcome=outcome)
    finally:
        await registry.close()
