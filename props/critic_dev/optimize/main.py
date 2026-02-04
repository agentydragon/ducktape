"""Prompt optimizer agent main entry point for in-container execution.

This is the CMD entrypoint for the prompt optimizer container. It:
1. Connects to backend REST API for orchestration tools (run_critic)
2. Polls database directly for grading status (wait_until_graded)
3. Renders the system prompt from Jinja2 template
4. Runs the agent loop until budget/timeout exhaustion or failure
5. Exits with appropriate code
"""

from __future__ import annotations

import asyncio
import logging
import sys

from agent_core.agent import Agent
from agent_core.handler import AbortIf, BaseHandler, RedirectOnTextMessageHandler
from agent_core.loop_control import AllowAnyToolOrTextMessage
from openai_utils.model import SystemMessage
from props.core.agent_helpers import get_current_agent_run
from props.core.eval_client import EvalClient
from props.core.loop_utils import create_bound_model_from_env, render_system_prompt, setup_crane_auth, setup_logging
from props.critic_dev.loop import TEXT_OUTPUT_REMINDER, LoggingHandler, LoopState, LoopStatus, create_tool_provider
from props.db.database import Database

logger = logging.getLogger(__name__)


async def run_prompt_optimizer_loop(
    system_prompt: str, eval_client: EvalClient, critic_model: str, db: Database
) -> int:
    """Run the prompt optimizer agent loop.

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    state = LoopState()
    tool_provider = create_tool_provider(state, eval_client, critic_model, db)

    bound_model = create_bound_model_from_env(db)

    handlers: list[BaseHandler] = [
        LoggingHandler(),
        RedirectOnTextMessageHandler(TEXT_OUTPUT_REMINDER),
        AbortIf(lambda: state.status != LoopStatus.IN_PROGRESS),
    ]

    agent = await Agent.create(
        tool_provider=tool_provider,
        handlers=handlers,
        client=bound_model,
        parallel_tool_calls=True,
        tool_policy=AllowAnyToolOrTextMessage(),
    )

    agent.process_message(SystemMessage.text(system_prompt))

    await agent.run()
    match state.status:
        case LoopStatus.IN_PROGRESS:
            # Ran until budget/timeout — that's success for PO
            logger.info("Optimization completed (exhausted budget/turns)")
            return 0
        case LoopStatus.EXITED_FAILURE:
            logger.info("Optimization failed")
            return 1


async def main() -> int:
    """Main entry point for prompt optimizer agent."""
    setup_logging()
    setup_crane_auth()

    logger.info("Prompt optimizer agent starting")
    db = Database.from_env()

    with db.session() as session:
        agent_run = get_current_agent_run(session)
        type_config = agent_run.prompt_optimizer_config()

    critic_model = type_config.critic_model

    logger.info("Connecting to backend REST API")
    async with EvalClient.from_env() as eval_client:
        logger.info("Connected to backend at %s", eval_client.backend_url)

        logger.info("Rendering system prompt")
        system_prompt = render_system_prompt("props/docs/agents/prompt_optimizer.md.j2", db)

        logger.info("Starting agent loop")
        exit_code = await run_prompt_optimizer_loop(system_prompt, eval_client, critic_model, db)

    logger.info("Agent loop finished with exit code %d", exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
