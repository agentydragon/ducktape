"""Improvement agent main entry point for in-container execution.

This is the CMD entrypoint for the improvement agent container. It:
1. Connects to host MCP server for orchestration tools (run_critic, run_grader)
2. Loads the system prompt from agent.md
3. Runs the agent loop until submit succeeds or failure
4. Exits with appropriate code
"""

from __future__ import annotations

import asyncio
import logging
import sys

from agent_pkg.runtime.mcp import mcp_client_from_env
from props.core.agent_helpers import get_current_agent_run
from props.core.db.config import get_database_config
from props.core.db.session import get_session
from props.core.loop_utils import render_system_prompt, setup_logging
from props.core.prompt_improve.loop import run_improvement_loop

logger = logging.getLogger(__name__)


async def main() -> int:
    """Main entry point for improvement agent."""
    setup_logging()

    logger.info("Improvement agent starting")

    # Get config from agent run
    with get_session() as session:
        agent_run = get_current_agent_run(session)
        agent_run_id = agent_run.agent_run_id
        type_config = agent_run.improvement_config()
        logger.info("Agent run: %s, model: %s", agent_run_id, agent_run.model)

    db_config = get_database_config()

    # Connect to host MCP server for orchestration tools
    logger.info("Connecting to host MCP server")
    async with mcp_client_from_env() as (mcp_client, init_result):
        logger.info(
            "Connected to MCP server: %s (version %s)",
            init_result.serverInfo.name,
            init_result.serverInfo.version,
        )

        # Render system prompt
        logger.info("Rendering system prompt")
        system_prompt = render_system_prompt("props/core/agent_defs/improvement/agent.md")

        # Run the agent loop
        logger.info("Starting agent loop")
        exit_code = await run_improvement_loop(
            system_prompt=system_prompt,
            mcp_client=mcp_client,
            agent_run_id=agent_run_id,
            type_config=type_config,
            db_config=db_config,
        )

    logger.info("Agent loop finished with exit code %d", exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
