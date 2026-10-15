"""The model name and reasoning effort Codex is launched with in tests."""

# Not in Codex's model catalog: a catalog model id switches Codex to code mode (one JS `exec` tool
# the model scripts against), while a routed or unknown id keeps the classic function-call shape.
MODEL = "agentplane-test-model"
EFFORT = "low"
