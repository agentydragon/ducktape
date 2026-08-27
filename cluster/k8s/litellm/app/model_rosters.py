"""Shared model rosters and lane-name derivations referenced by LiteLLM cross-configuration tests.

Lane naming (#4823): an exposed `model_name` is `{lane}-{upstream model}`. The lane
prefix names the upstream account, extended with the wire surface where one account is
exposed on more than one wire:

- `chatgpt-messages-*` — ChatGPT/Codex subscription via CLIProxyAPI, Anthropic Messages
  wire (Claude Code clients)
- `chatgpt-responses-*` — the same account, OpenAI Responses wire (Codex clients)
- `tana-*` — Tana account via tana-litellm
- `google-*` — Google AI key (Gemini chat + embeddings)

The lane rides in front, not behind, because key allowlists match `model_name` prefixes
(the `claude-*` wildcard in tf/gitops/litellm-keys/main.tf): a suffix-shaped Anthropic
lane name would begin with `claude-` and silently join every `claude-*` allowlist.
Deliberately bare: the direct-API `claude-*` entries (Claude Code names those slugs
itself, and the client keys' `claude-*` wildcard admits them), the groq entries, and the
self-hosted Ollama entries, whose `-openai-chat`/`-ollama-native` wire suffixes have no
account to name.
"""


def chatgpt_messages_name(model: str) -> str:
    """Anthropic Messages lane via CLIProxyAPI — the wire Claude Code clients speak."""
    return f"chatgpt-messages-{model}"


def chatgpt_responses_name(model: str) -> str:
    """OpenAI Responses lane via CLIProxyAPI — the wire Codex clients speak."""
    return f"chatgpt-responses-{model}"


def google_name(model: str) -> str:
    return f"google-{model}"


# CLEANUP(added 2026-08-27): pre-#4823 lane names, still what every deployed consumer
# calls (haku-console config.yaml, the baked workspace/codex-pod images, openclaw.json,
# props config.toml, laptop wrappers). Consumers move lane by lane under #4823; when a
# lane's last consumer moves, drop its legacy derivation together with its
# proxy-config.yaml entries and tf/gitops/litellm-keys allowlist rows.
def legacy_messages_name(model: str) -> str:
    """Pre-#4823 Messages-lane name — the #4822 trap: says Codex, serves Claude Code."""
    return f"codex-{model}"


def legacy_responses_name(model: str) -> str:
    """Pre-#4823 Responses-lane name — the suffix names the account, not the wire."""
    return f"{model}-chatgpt"


def legacy_google_name(model: str) -> str:
    """Pre-#4823 Google-lane name — the bare upstream id, no lane marker."""
    return model


# ChatGPT/Codex-subscription models behind CLIProxyAPI, each exposed on both wire
# surfaces (the chatgpt-* lane derivations above).
CLIPROXY_MODELS: list[str] = [
    "gpt-5.4",
    "gpt-5.5",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.3-codex-spark",
]

# Real Anthropic API roster verified against the authenticated /v1/models
# endpoint. These names are mirrored into Haku OpenClaw and Terraform.
ANTHROPIC_MODELS: list[str] = ["claude-opus-5", "claude-sonnet-5", "claude-fable-5", "claude-haiku-4-5-20251001"]

# The subset exposed in OpenClaw's model picker. OpenClaw's bundled LiteLLM
# provider does not query the proxy's authenticated /v1/models endpoint.
OPENCLAW_CLIPROXY_MODELS: list[str] = ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"]
OPENCLAW_CODEX_MODELS: list[str] = [legacy_messages_name(model) for model in OPENCLAW_CLIPROXY_MODELS]

# Google AI (Gemini). Key from the GEMINI_API_KEY env var (litellm-gemini-key
# secret). Current-generation lineup only (Gemini 3.x) -- the 2.5 generation,
# the shut-down gemini-3-pro-preview, and every non-latest 3.x minor version
# (gemini-3-flash-preview, 3.5-flash, 3.6-flash, 3.1-flash-lite) are dropped,
# the same "only the newest group" policy as OPENCLAW_CODEX_MODELS above. The
# gemini-pro-latest/gemini-flash-latest floating aliases are dropped too --
# redundant with pinning the current models explicitly. Verified against
# ai.google.dev/gemini-api/docs/models (2026-08-23): Pro is frozen at 3.1
# (preview) since February 2026 -- no 3.5/3.6/3.7 Pro exists -- while Flash
# advanced through 3.5 -> 3.6 -> 3.7 (shipped 2026-08-13) on an independent,
# faster cadence; 3.5-flash-lite is the current lite tier. Remaining specialty
# SKUs (image, tts, live/bidi, customtools, imagen, veo, "Cyber") are
# intentionally excluded — add on demand. Mirrored into the gemini-clients
# Terraform key and public-coder-agent's OpenClaw catalog; both are pinned
# against this list.
GEMINI_MODELS: list[str] = ["gemini-3.1-pro-preview", "gemini-3.7-flash", "gemini-3.5-flash-lite"]

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

# Published input/output token limits shared across the current Gemini chat
# generation: ai.google.dev/gemini-api/docs/models/gemini-3.1-pro-preview,
# .../gemini-3.7-flash, and .../gemini-3.5-flash-lite (2026-08-23). Unlike
# Codex's CODEX_CONTEXT_WINDOW below, there is no live serving-path probe for
# a third-party hosted API, so this is Google's published figure rather than
# a measured one. Used by public-coder-agent's OpenClaw catalog.
GEMINI_CONTEXT_WINDOW = 1_048_576
GEMINI_MAX_OUTPUT_TOKENS = 65_536

# The "-lite" tier is the deliberately cheap/fast one; Pro and plain Flash are
# both positioned around their reasoning ("thinking") capability, Pro's
# effectively mandatory. Google's own gemini-3.7-flash page notes thinking is
# supported but not automatic-by-default -- this tracks capability/product
# positioning, not the literal default toggle.
GEMINI_NON_REASONING_MODELS: frozenset[str] = frozenset({"gemini-3.5-flash-lite"})
