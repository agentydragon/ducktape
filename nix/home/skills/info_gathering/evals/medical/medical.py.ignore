"""Medical diagnosis eval variants.

Usage:
  bazel run //nix/home/skills/info_gathering/evals/medical -- --api-key KEY --variant iih
  bazel run //nix/home/skills/info_gathering/evals/medical -- --api-key KEY --variant gerd
"""

import argparse
import logging
from dataclasses import dataclass

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

_SIM_RLOCATIONS: dict[str, str] = {
    "iih": "_main/nix/home/skills/info_gathering/evals/medical/sim_iih.txt",
    "gerd": "_main/nix/home/skills/info_gathering/evals/medical/sim_gerd.txt",
}

AGENT_EXTRA_SYSTEM = (
    "You are a diagnostician.\n"
    "- History questions are FREE — ask many before ordering tests\n"
    "- You may order multiple tests per turn; note which are parallelizable\n"
    "- State diagnosis as: 'My diagnosis is: [DIAGNOSIS]'"
)


@dataclass
class Variant:
    presenting_complaint: str
    turn_limit: int = 15


VARIANTS: dict[str, Variant] = {
    "iih": Variant(
        presenting_complaint=(
            "I've been getting really bad headaches for the past few weeks, and my vision has been getting blurry."
        )
    ),
    "gerd": Variant(
        presenting_complaint=(
            "I've been having chest pain and I can't catch my breath. I'm really scared it might be my heart."
        )
    ),
}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    p = argparse.ArgumentParser(description="Medical diagnosis eval")
    add_common_args(p)
    p.add_argument("--variant", choices=list(VARIANTS), required=True)
    args = p.parse_args()

    v = VARIANTS[args.variant]
    name = f"medical_{args.variant}"

    skill_text = load_skill()
    agent_system = build_agent_system(skill_text, AGENT_EXTRA_SYSTEM)
    client = make_client()
    thinking = thinking_from_args(args)
    output_dir = output_dir_from_args(args)

    sim_system = get_required_path(_SIM_RLOCATIONS[args.variant]).read_text()

    logger.info("=" * 60)
    logger.info("  %s  |  %s  |  thinking=%s", name, args.model, thinking or "off")
    logger.info("=" * 60)

    summary = run_conversation_eval(
        name=name,
        client=client,
        model=args.model,
        agent_system=agent_system,
        first_user_message=v.presenting_complaint,
        sim_system=sim_system,
        sim_tools=[END_GAME_TOOL],
        turn_limit=v.turn_limit,
        thinking_budget=thinking,
        output_dir=output_dir,
    )
    logger.info("%s", summary)


if __name__ == "__main__":
    main()
