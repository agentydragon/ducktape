from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from inop.prompting.prompt_engineer import FeedbackProvider
from mcp_infra.enhanced.server import EnhancedFastMCP
from openai_utils.pydantic_strict_mode import OpenAIStrictModeBaseModel

# ---- Dependencies and state -------------------------------------------------


class PromptEvaluationDeps(Protocol):
    async def select_seed_tasks(self) -> list[Any]: ...
    async def run_rollouts_with_prompt(self, prompt: str, tasks: list[Any]) -> list[Any]: ...
    def persist_all(self, *, iteration: int, prompt: str, rollouts: list[Any], feedback: str) -> None: ...


@dataclass
class PromptFeedbackState:
    iteration: int = 0
    last_prompt: str | None = None
    last_feedback: str = ""


logger = logging.getLogger(__name__)


class ProposePromptInput(OpenAIStrictModeBaseModel):
    """Input for propose_prompt tool."""

    prompt: str


def make_prompt_feedback_server_with_state(
    deps: PromptEvaluationDeps, feedback_provider: FeedbackProvider
) -> tuple[EnhancedFastMCP, PromptFeedbackState]:
    """FastMCP with closure state (tools-builder style). No lifespan/ContextVar.

    Returns (server, state). Attach via mcp.attach_inproc.
    """
    state = PromptFeedbackState()
    mcp = EnhancedFastMCP(
        "Prompt Evaluation MCP Server", instructions="Prompt evaluation (rollouts+grading+persistence)"
    )

    @mcp.flat_model()
    async def propose_prompt(input: ProposePromptInput) -> dict[str, str]:
        logger.info("propose_prompt: start", extra={"prompt": input.prompt})
        state.iteration += 1
        state.last_prompt = input.prompt
        tasks = await deps.select_seed_tasks()
        rollouts = await deps.run_rollouts_with_prompt(input.prompt, tasks)
        # Let the configured provider compute feedback (may grade/aggregate internally)
        feedback = await feedback_provider.provide_feedback(rollouts)
        logger.info("propose_prompt: about to persist", extra={"iter": state.iteration})
        deps.persist_all(iteration=state.iteration, prompt=input.prompt, rollouts=rollouts, feedback=feedback)
        logger.info("propose_prompt: persisted", extra={"iter": state.iteration})
        state.last_feedback = feedback
        return {"feedback": feedback}

    return mcp, state
