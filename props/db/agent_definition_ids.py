"""Constants for agent image references.

These are short-name references for built-in agents that resolve to OCI image digests.
The actual agent definitions live in props/core/agent_defs/<name>/.
"""

# Core evaluation agents
CRITIC_IMAGE_REF: str = "critic"
GRADER_IMAGE_REF: str = "grader"

# Optimization agents
PROMPT_OPTIMIZER_IMAGE_REF: str = "prompt_optimizer"
IMPROVEMENT_IMAGE_REF: str = "improvement"
