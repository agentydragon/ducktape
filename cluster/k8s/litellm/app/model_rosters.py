"""Shared model rosters referenced by LiteLLM cross-configuration tests."""

# Real Anthropic API roster verified against the authenticated /v1/models
# endpoint. These names are mirrored into Haku OpenClaw and Terraform.
ANTHROPIC_MODELS: list[str] = ["claude-opus-5", "claude-sonnet-5", "claude-fable-5", "claude-haiku-4-5-20251001"]

# The subset exposed in OpenClaw's model picker. OpenClaw's bundled LiteLLM
# provider does not query the proxy's authenticated /v1/models endpoint.
OPENCLAW_CODEX_MODELS: list[str] = ["codex-gpt-5.6-luna", "codex-gpt-5.6-terra", "codex-gpt-5.6-sol"]

# Google AI (Gemini). Key from the GEMINI_API_KEY env var (litellm-gemini-key
# secret). Chat lineup verified against generativelanguage.googleapis.com/v1beta/models
# (2026-07-18): the Gemini-3.x preview family (pro / flash / flash-lite) + gemini-3.5-flash,
# the stable 2.5 pair, and the -latest aliases that auto-point at the newest generation.
# Remaining specialty SKUs (image, tts, live/bidi, customtools, imagen, veo) are
# intentionally excluded — add on demand. Mirrored into the gemini-clients Terraform
# key and public-coder-agent's OpenClaw catalog; both are pinned against this list.
GEMINI_MODELS: list[str] = [
    "gemini-3-pro-preview",
    "gemini-3-flash-preview",
    "gemini-3.1-pro-preview",
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-pro-latest",
    "gemini-flash-latest",
]

# Gemini embeddings, same key as the chat lineup. Added for OpenClaw memory search,
# whose index needs an embedding backend and had none — see
# plans/personal_agents/findings/harness_behaviour.md F9.
#
# Both are stable and their embedding spaces are **mutually incompatible**: vectors
# from one cannot be compared against the other, so switching a consumer between
# them means re-embedding its whole corpus. Verified against
# ai.google.dev/gemini-api/docs/embeddings (2026-07-30).
#
# gemini-embedding-2 is the current one (8,192 input tokens, multimodal);
# gemini-embedding-001 is text-only with a 2,048-token limit and is kept because it
# is the longer-established route through LiteLLM. Both expose flexible output
# dimensionality (128-3072, recommended 768/1536/3072), selected per request rather
# than per deployment, so neither entry pins a size.
GEMINI_EMBEDDING_MODELS: list[str] = ["gemini-embedding-2", "gemini-embedding-001"]

# Published input/output token limits shared by the whole current Gemini chat
# generation (2.5 and 3.x alike): ai.google.dev/gemini-api/docs/models/gemini-3.1-pro-preview
# and ai.google.dev/gemini-api/docs/gemini-3 (2026-08-23). Unlike Codex's
# CODEX_CONTEXT_WINDOW below, there is no live serving-path probe for a
# third-party hosted API, so this is Google's published figure rather than a
# measured one. Used by public-coder-agent's OpenClaw catalog.
GEMINI_CONTEXT_WINDOW = 1_048_576
GEMINI_MAX_OUTPUT_TOKENS = 65_536

# The "-lite" tier ships with thinking off by default (Google's positioning: the
# lowest-latency, highest-throughput tier); every other current Gemini chat model
# is a hybrid reasoning model with thinking on by default.
GEMINI_NON_REASONING_MODELS: frozenset[str] = frozenset({"gemini-3.1-flash-lite"})
