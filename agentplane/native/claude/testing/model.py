"""The model name Claude Code is launched with in tests."""

# A routed name in the shape a LiteLLM deployment gives Claude Code; the family suffix lets it
# resolve the model's context window. The scripted upstream never dispatches on it.
MODEL = "agentplane-test/claude-haiku-4-5-20251001"
