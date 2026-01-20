"""Grader agent main entry point for in-container execution (one-off mode).

This is the CMD entrypoint for the one-off grader container. It:
1. Fetches the snapshot to /workspace
2. Renders the system prompt
3. Runs the agent loop until submit succeeds or failure
4. Exits with appropriate code
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from props.core.agent_helpers import fetch_snapshot, get_current_agent_run
from props.core.agent_template import render_system_prompt
from props.core.db.session import get_session
from props.core.grader.loop import GraderMode, run_grader_loop

logger = logging.getLogger(__name__)

WORKSPACE = Path("/workspace")


async def main() -> int:
    """Main entry point for one-off grader agent.

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    logger.info("Grader agent starting (one-off mode)")

    # Get model and snapshot from agent run config
    with get_session() as session:
        agent_run = get_current_agent_run(session)
        model = agent_run.model
        config = agent_run.grader_config()
        # Get snapshot slug from the critic run being graded
        critic_run = session.get_one(type(agent_run), config.graded_agent_run_id)
        snapshot_slug = critic_run.critic_config().example.snapshot_slug
        logger.info("Agent run: %s, model: %s, snapshot: %s", agent_run.agent_run_id, model, snapshot_slug)

    # Fetch snapshot
    logger.info("Fetching snapshot to %s", WORKSPACE)
    fetch_snapshot(WORKSPACE)

    # Render system prompt
    logger.info("Rendering system prompt")
    system_prompt = render_system_prompt("props/docs/agents/grader.md.j2")

    # Run the agent loop
    logger.info("Starting agent loop")
    exit_code = await run_grader_loop(system_prompt, model, snapshot_slug, GraderMode.ONE_OFF)

    logger.info("Agent loop finished with exit code %d", exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
