"""Improvement agent: creates definitions to beat baseline on allowed examples.

New architecture (in-container agent loop):
- Agent loop runs inside the container via CMD entrypoint
- Host serves PromptEvalServer via HTTP for orchestration tools (run_critic, run_grader)
- Container connects to host MCP server via MCP_SERVER_URL/MCP_SERVER_TOKEN
- Container exits 0 on success (submit), non-zero on failure (report_failure)
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path  # Used for output_dir
from typing import Annotated, Literal
from uuid import UUID, uuid4

import aiodocker
from pydantic import BaseModel, Field

from openai_utils.model import OpenAIModelProto
from props.core.agent_registry import AgentRegistry
from props.core.agent_types import AgentType, ImprovementTypeConfig
from props.core.db.config import DatabaseConfig
from props.core.db.models import AgentRun, AgentRunStatus
from props.core.db.session import get_session
from props.core.display import short_uuid
from props.core.loop_agent_env import run_loop_agent
from props.core.mcp_http_server import serve_mcp_http
from props.core.models.examples import ExampleSpec
from props.core.oci_utils import BUILTIN_TAG, build_oci_reference, resolve_image_ref
from props.core.prompt_improve.loop import TerminationSuccess
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


# Default LLM proxy URL (same as agent_registry default)
DEFAULT_LLM_PROXY_URL = "http://props-llm-proxy:5052"


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
    llm_proxy_url: str = DEFAULT_LLM_PROXY_URL,
) -> ImprovementResult:
    """Run improvement agent with in-container agent loop.

    New architecture:
    - Agent loop runs inside the container (not on host)
    - Host serves PromptEvalServer via HTTP for orchestration tools
    - Container connects to host MCP server via MCP_SERVER_URL/MCP_SERVER_TOKEN
    - Container exits 0 on success, non-zero on failure
    """
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

    # Create registry for critic/grader runs initiated by the improvement agent
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
        target_metric=TargetMetric.TARGETED,
        optimizer_run_id=run_id,
        verbose=verbose,
    )

    try:
        # Serve PromptEvalServer via HTTP for container to connect
        with serve_mcp_http(prompt_eval_server) as mcp_handle:
            logger.info(f"MCP server available at {mcp_handle.url}")

            # Run the container with in-container agent loop
            result = await run_loop_agent(
                docker_client=docker_client,
                agent_run_id=run_id,
                db_config=db_config,
                image=image,
                llm_proxy_url=llm_proxy_url,
                extra_env={
                    "MCP_SERVER_URL": mcp_handle.url,
                    "MCP_SERVER_TOKEN": mcp_handle.token,
                },
                container_name=f"improve-{short_uuid(run_id)}",
            )

            logger.info(f"Container exited with code {result.exit_code}")
            if verbose and result.stderr:
                logger.info(f"Container stderr:\n{result.stderr}")

        # Update status based on exit code
        final_status = AgentRunStatus.COMPLETED if result.exit_code == 0 else AgentRunStatus.REPORTED_FAILURE
        with get_session() as session:
            agent_run = session.get(AgentRun, run_id)
            if agent_run:
                agent_run.status = final_status
                agent_run.container_exit_code = result.exit_code
                session.commit()
                logger.info(f"Updated agent_run status to {final_status.value}")

        # Determine outcome
        outcome: ImprovementOutcome
        if result.exit_code == 0:
            # Success - for now just return exhausted (container writes details to DB)
            outcome = OutcomeExhausted()  # TODO: Parse actual success details from DB
        else:
            outcome = OutcomeUnexpectedTermination(
                message=f"Container exited with code {result.exit_code}"
            )

        logger.info(f"Improvement agent completed: kind={outcome.kind}")
        return ImprovementResult(tokens_used=0, run_id=run_id, outcome=outcome)  # TODO: Track tokens

    finally:
        await registry.close()
