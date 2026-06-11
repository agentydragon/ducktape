"""Constants for agent image references.

These are short-name references for built-in agents that resolve to OCI image digests.
The agent implementations live in their respective packages:
- props/core/critic/
- props/core/grader/
- props/core/prompt_optimize/
- props/core/prompt_improve/
"""

# Core evaluation agents
CRITIC_IMAGE_REF: str = "critic"
GRADER_IMAGE_REF: str = "grader"

# Optimization agents
PROMPT_OPTIMIZER_IMAGE_REF: str = "prompt_optimizer"
IMPROVEMENT_IMAGE_REF: str = "improvement"
