"""Tech support anchoring trap eval.

Usage:
  bazel run //nix/home/skills/info_gathering/evals/tech_anchoring -- --api-key KEY
"""

import argparse
import logging

from nix.home.skills.info_gathering.evals.harness import (
    END_GAME_TOOL,
    add_common_args,
    build_agent_system,
    load_skill,
    make_client,
    output_dir_from_args,
    run_conversation_eval,
    thinking_from_args,
)
from util.bazel.runfiles import get_required_path

logger = logging.getLogger(__name__)

NAME = "tech_anchoring"
TURN_LIMIT = 10

_SIM_RLOCATION = "_main/nix/home/skills/info_gathering/evals/tech_anchoring/sim.txt"

FIRST_MESSAGE = (
    "My internet has been really slow for the past day. I installed a new "
    "browser extension yesterday. Can you help me figure out what's going on?"
)

AGENT_EXTRA_SYSTEM = (
    "Diagnose this tech problem.\n"
    "- The user can check things physically\n"
    "- State diagnosis as: 'My diagnosis is: [CAUSE]'"
)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    p = argparse.ArgumentParser(description="Tech anchoring eval")
    add_common_args(p)
    args = p.parse_args()

    skill_text = load_skill()
    agent_system = build_agent_system(skill_text, AGENT_EXTRA_SYSTEM)
    client = make_client()
    thinking = thinking_from_args(args)
    output_dir = output_dir_from_args(args)

    sim_system = get_required_path(_SIM_RLOCATION).read_text()

    logger.info("=" * 60)
    logger.info("  %s  |  %s  |  thinking=%s", NAME, args.model, thinking or "off")
    logger.info("=" * 60)

    summary = run_conversation_eval(
        name=NAME,
        client=client,
        model=args.model,
        agent_system=agent_system,
        first_user_message=FIRST_MESSAGE,
        sim_system=sim_system,
        sim_tools=[END_GAME_TOOL],
        turn_limit=TURN_LIMIT,
        thinking_budget=thinking,
        output_dir=output_dir,
    )
    logger.info("%s", summary)


if __name__ == "__main__":
    main()
