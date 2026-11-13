"""Apartment search preference recovery eval.

Usage:
  bazel run //nix/home/skills/info_gathering/evals/apartments -- --api-key KEY
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

NAME = "apartments"
TURN_LIMIT = 12

_FIRST_MESSAGE_RLOCATION = "_main/nix/home/skills/info_gathering/evals/apartments/first_message.txt"
_SIM_RLOCATION = "_main/nix/home/skills/info_gathering/evals/apartments/sim.txt"

AGENT_EXTRA_SYSTEM = (
    "Help the user choose an apartment.\n"
    "- Their preferences are UNKNOWN — you must elicit them\n"
    "- Final answer: 'My ranking: [best] > [next] > ...'"
)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    p = argparse.ArgumentParser(description="Apartment search eval")
    add_common_args(p)
    args = p.parse_args()

    skill_text = load_skill()
    agent_system = build_agent_system(skill_text, AGENT_EXTRA_SYSTEM)
    client = make_client()
    thinking = thinking_from_args(args)
    output_dir = output_dir_from_args(args)

    first_user_message = get_required_path(_FIRST_MESSAGE_RLOCATION).read_text()
    sim_system = get_required_path(_SIM_RLOCATION).read_text()

    logger.info("=" * 60)
    logger.info("  %s  |  %s  |  thinking=%s", NAME, args.model, thinking or "off")
    logger.info("=" * 60)

    summary = run_conversation_eval(
        name=NAME,
        client=client,
        model=args.model,
        agent_system=agent_system,
        first_user_message=first_user_message,
        sim_system=sim_system,
        sim_tools=[END_GAME_TOOL],
        turn_limit=TURN_LIMIT,
        thinking_budget=thinking,
        output_dir=output_dir,
    )
    logger.info("%s", summary)


if __name__ == "__main__":
    main()
