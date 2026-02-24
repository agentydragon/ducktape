#!/usr/bin/env python3
"""TEMPORARY ONE-OFF quick test with reduced parameters.

Can delete after: Verification complete
"""

import asyncio
import os
import sys

# Temporarily modify parameters for quick test
sys.path.insert(0, ".")
import oneoff__llm_rl_experiment as main_exp

# Override parameters for quick test
main_exp.MODELS = ["gpt-4o-mini", "gpt-4o", "o1-mini"]
main_exp.ENVIRONMENTS = ["FrozenLake-v1", "CartPole-v1"]  # Just 2 envs
main_exp.EPISODES_PER_RUN = 2  # Just 2 episodes
main_exp.RUNS_PER_EXPERIMENT = 2  # Just 2 runs
main_exp.MAX_CONCURRENT_EXPERIMENTS = 3  # Lower concurrency


async def quick_test():
    """Run a quick test to verify all models work."""
    print("=== QUICK TEST MODE ===")
    print(f"Models: {main_exp.MODELS}")
    print(f"Environments: {main_exp.ENVIRONMENTS}")
    print(f"Episodes per run: {main_exp.EPISODES_PER_RUN}")
    print(f"Runs per experiment: {main_exp.RUNS_PER_EXPERIMENT}")
    print(f"Total experiments: {len(main_exp.MODELS) * len(main_exp.ENVIRONMENTS) * main_exp.RUNS_PER_EXPERIMENT}")

    # Check API key
    if not os.getenv("OPENAI_API_KEY"):
        print("Error: OPENAI_API_KEY environment variable not set")
        return

    await main_exp.main()


if __name__ == "__main__":
    asyncio.run(quick_test())
