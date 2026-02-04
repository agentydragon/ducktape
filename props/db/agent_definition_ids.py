"""Constants for agent image references.

These are short-name references for built-in agents that resolve to OCI image digests.
The agent implementations live in their respective packages:
- props/core/critic/
- props/core/grader/
- props/core/prompt_optimize/
- props/core/prompt_improve/

All builtins use the same ref "builtin" which maps to BUILTIN_TAG ("latest")
in the agent registry's image resolution.
"""

# Canonical ref for all built-in agent images.
# Resolved to BUILTIN_TAG ("latest") by AgentRegistry._resolve_image_ref.
BUILTIN_IMAGE_REF: str = "builtin"

# Backwards-compatible aliases (all resolve the same way)
CRITIC_IMAGE_REF: str = BUILTIN_IMAGE_REF
GRADER_IMAGE_REF: str = BUILTIN_IMAGE_REF
PROMPT_OPTIMIZER_IMAGE_REF: str = BUILTIN_IMAGE_REF
IMPROVEMENT_IMAGE_REF: str = BUILTIN_IMAGE_REF
