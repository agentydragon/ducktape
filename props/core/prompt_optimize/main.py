"""Prompt optimizer agent main entry point for in-container execution.

This is the CMD entrypoint for the prompt optimizer container. It:
1. Connects to host MCP server for orchestration tools (run_critic, run_grader)
2. Renders the system prompt
3. Runs the agent loop until submit succeeds or failure
4. Exits with appropriate code
"""

from __future__ import annotations

import asyncio
import logging
import sys

from agent_pkg.runtime.mcp import mcp_client_from_env
from props.core.loop_utils import render_system_prompt, setup_logging
from props.core.prompt_optimize.loop import run_prompt_optimizer_loop

logger = logging.getLogger(__name__)


async def main() -> int:
    """Main entry point for prompt optimizer agent."""
    setup_logging()

    logger.info("Prompt optimizer agent starting")

    # Connect to host MCP server for orchestration tools
    logger.info("Connecting to host MCP server")
    async with mcp_client_from_env() as (mcp_client, init_result):
        logger.info(
            "Connected to MCP server: %s (version %s)", init_result.serverInfo.name, init_result.serverInfo.version
        )

        # Render system prompt
        logger.info("Rendering system prompt")
        system_prompt = render_system_prompt("props/docs/agents/prompt_optimizer.md.j2")

        # Run the agent loop
        logger.info("Starting agent loop")
        exit_code = await run_prompt_optimizer_loop(system_prompt, mcp_client)

    logger.info("Agent loop finished with exit code %d", exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
