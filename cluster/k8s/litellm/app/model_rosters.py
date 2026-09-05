"""Shared model rosters and exposed-name derivations referenced by LiteLLM cross-configuration tests.

Naming scheme (#4823): an exposed `model_name` is `{provider}/{shape}/{model}` — the
upstream account/provider, the API shape LiteLLM exposes the entry under, then the
upstream model:

- `chatgpt/ant-messages/*` / `chatgpt/oai-responses/*` — ChatGPT/Codex subscription via
  CLIProxyAPI, on the Anthropic Messages wire (Claude Code clients) and the OpenAI
  Responses wire (Codex clients)
- `anthropic-max20/ant-messages/*` — Claude Code subscription via CLIProxyAPI's Claude
  OAuth session, on the Anthropic Messages wire (a different upstream session on the same
  pod as `chatgpt/*`, distinct from the direct-API `anthropic-api/ant-messages/*` entries)
- `anthropic-api/ant-messages/*` — direct Anthropic API on the Anthropic Messages wire
- `tana/ant-messages/*` — Tana account via tana-litellm, an Anthropic Messages
  passthrough
- `google/oai-chat/*` / `google/oai-embeddings/*` — Google AI key (Gemini)
- `mistral/oai-chat/*` — Mistral API key

A shape slug is `<definer>-<protocol>` (ant-messages, oai-responses, oai-chat,
oai-embeddings): the shape segment names a wire protocol, and wire protocols are
identified by their definer — the bare nouns are unique only in today's snapshot
("chat" and "embeddings" are already generic: Cohere chat and Google embedContent are
distinct wire shapes answering to the same nouns). The definer prefix is NOT the
provider segment: provider says whose ACCOUNT serves the entry, the definer says whose
PROTOCOL it speaks, and they vary independently — `chatgpt/ant-messages/*` is the
ChatGPT account serving Anthropic's wire shape. Definer slugs stay short and fixed
(ant, oai; future goog, coh, ...) so a provider slug can never stutter against a
definer name (openai/openai-chat).

Segments are separated by `/`, not `-`: provider model slugs are dash-heavy
(gpt-5.6-sol, claude-sonnet-4-6, gemini-embedding-001), so a dash cannot mark segment
boundaries unambiguously. `/` is LiteLLM's own model-group idiom (its docs' recommended
`model_name: openai/gpt-4o`, wildcard `openai/*`) and is already served in-cluster by
tana-litellm (`claude-sonnet-4-6/medium`, `gpt-5.1/medium`); clients carry the model in
the request body (Claude Code, Codex, OpenClaw), and on this stack a model name never
rides in a URL path or a Kubernetes resource name.

The provider segment rides in front, not behind, because key allowlists match
`model_name` prefixes (the `anthropic-api/ant-messages/*` wildcard in
tf/gitops/litellm-keys/main.tf). Deliberately not renamed: the raw upstream model slugs
inside the exposed names, the groq entries, and the self-hosted Ollama entries, whose
`-openai-chat`/`-ollama-native` wire suffixes have no account to name.
"""

from enum import StrEnum


class Provider(StrEnum):
    """First scheme segment: the upstream account/provider an entry spends from."""

    CHATGPT = "chatgpt"
    ANTHROPIC_API = "anthropic-api"
    ANTHROPIC_MAX20 = "anthropic-max20"
    TANA = "tana"
    GOOGLE = "google"
    MISTRAL = "mistral"


class ApiShape(StrEnum):
    """Second scheme segment: the API shape LiteLLM exposes the entry under, as `<definer>-<protocol>`."""

    ANT_MESSAGES = "ant-messages"
    OAI_RESPONSES = "oai-responses"
    OAI_CHAT = "oai-chat"
    OAI_EMBEDDINGS = "oai-embeddings"


def exposed_name(provider: Provider, shape: ApiShape, model: str) -> str:
    """#4823 scheme name, e.g. `chatgpt/oai-responses/gpt-5.6-luna`."""
    return f"{provider}/{shape}/{model}"


# ChatGPT/Codex-subscription models behind CLIProxyAPI, exposed on both wire surfaces
# for clients that need them. OpenClaw uses the Responses surface below because it is
# the working native passthrough to CLIProxyAPI.
CLIPROXY_MODELS: list[str] = [
    "gpt-6-astra",
    "gpt-5.4",
    "gpt-5.5",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.3-codex-spark",
]

# Context window + max output tokens for the Codex-subscription models. Measured,
# not published: litellm's model_cost DB (live-fetched from BerriAI) has exact
# entries for the real OpenAI models at their raw-API windows -- gpt-5.6-{sol,terra,
# luna} at 922K, gpt-5.4/5.5 at 1.05M -- and Codex product docs say 272K, but none
# is what this subscription path (client -> LiteLLM -> CLIProxyAPI -> upstream)
# actually serves. So the openai/-prefixed routes advertise litellm's raw-API window
# (it has no entry for the anthropic/-prefixed twins -> null); this measured value is
# the SSOT the LiteLLM config injects into model_info (test_litellm_config.py).
#
# openai_utils/probe_context_window.py binary-searches the live path. On 2026-07-29
# all three 5.6 models behaved identically: 370,629 tokens accepted, 372,194
# rejected. Re-derive with:
#
#     kubectl exec -i -n <ns> <pod> -- python3 - --low 350000 --high 400000 \
#         chatgpt/ant-messages/gpt-5.6-{luna,sol,terra} < openai_utils/probe_context_window.py
CODEX_CONTEXT_WINDOW = 372_000
CODEX_MAX_TOKENS = 128_000

# Codex 0.153.4's bundled Astra metadata permits model_context_window up to
# 872k. Advertise that maximum instead of Codex's conservative 272k default;
# the raw API advertises a 1.05M combined window and 128k maximum output.
ASTRA_CONTEXT_WINDOW = 872_000
ASTRA_MAX_TOKENS = 128_000

# Only the probed 5.6 models carry the measured window in the LiteLLM manifest;
# gpt-5.4/5.5/5.3-codex-spark were never probed and are left without model_info
# token limits. A newly added 5.6 model must be probed before being added here.
CODEX_MEASURED_MODELS: list[str] = ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]

# Tana-UI models fronted through tana-litellm. Tana encodes reasoning effort in the
# model name (`/medium`, `/high`), not a `reasoning_effort` param, so there is no clean
# "one model + effort knob" to map onto; we expose one model per family at its default
# effort. Each entry: (exposed-name base, tana-litellm downstream model_name). The
# downstream name's slash stays inside the `anthropic/` arg, never exposed.
TANA_MODELS: list[tuple[str, str]] = [
    ("claude-sonnet-4-6", "claude-sonnet-4-6/medium"),
    ("claude-opus-4-6", "claude-opus-4-6/high"),
    ("claude-haiku-4-5", "claude-haiku-4-5-20251001"),
]

# Current-generation Anthropic roster, verified against the authenticated /v1/models
# endpoint. Mirrored into Haku OpenClaw and Terraform, and reused as the exposed set for
# the cliproxyapi Claude-subscription `anthropic-max20/ant-messages/*` route: cliproxyapi's Claude
# OAuth session serves older generations too, but we expose only this current group — the
# subscription and the direct API serve the same current models, and sharing one list
# keeps them in sync ("newest group only", as with the Gemini roster).
ANTHROPIC_MODELS: list[str] = ["claude-opus-5", "claude-sonnet-5", "claude-fable-5", "claude-haiku-4-5-20251001"]

# The subset exposed in OpenClaw's model picker and the serving-path limits it
# must declare because OpenClaw's bundled LiteLLM provider does not query the
# proxy's authenticated /v1/models endpoint.
OPENCLAW_CLIPROXY_MODEL_LIMITS: dict[str, tuple[int, int]] = {
    "gpt-6-astra": (ASTRA_CONTEXT_WINDOW, ASTRA_MAX_TOKENS),
    "gpt-5.6-luna": (CODEX_CONTEXT_WINDOW, CODEX_MAX_TOKENS),
    "gpt-5.6-terra": (CODEX_CONTEXT_WINDOW, CODEX_MAX_TOKENS),
    "gpt-5.6-sol": (CODEX_CONTEXT_WINDOW, CODEX_MAX_TOKENS),
}
OPENCLAW_CODEX_MODELS: list[str] = [
    exposed_name(Provider.CHATGPT, ApiShape.OAI_RESPONSES, model) for model in OPENCLAW_CLIPROXY_MODEL_LIMITS
]

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
# intentionally excluded — add on demand. `gemini-3.1-pro-preview` was tested
# on 2026-08-30 and did not work with the current credential: Google returned
# RESOURCE_EXHAUSTED with a quota of 0. It may simply have no quota, but keep it
# out of the roster until that is verified. Mirrored into the gemini-clients
# Terraform key and public-coder-agent's OpenClaw catalog; both are pinned
# against this list.
GEMINI_MODELS: list[str] = ["gemini-3.7-flash", "gemini-3.5-flash-lite"]

# Mistral chat models that accepted a minimal completion with the cluster's API
# key on 2026-08-31. Catalog entries that returned 403 are intentionally
# excluded; account-specific fine-tuned models are excluded as well.
MISTRAL_MODELS: list[str] = [
    "codestral-2508",
    "codestral-latest",
    "magistral-medium-latest",
    "magistral-small-latest",
    "ministral-14b-latest",
    "ministral-14b-2512",
    "ministral-8b-latest",
    "ministral-8b-2512",
    "ministral-3b-latest",
    "ministral-3b-2512",
    "mistral-code-fim-latest",
    "mistral-code-latest",
    "mistral-medium",
    "mistral-medium-2604",
    "mistral-medium-3",
    "mistral-medium-3-5",
    "mistral-medium-3.5",
    "mistral-medium-latest",
    "mistral-small-2603",
    "mistral-small-latest",
    "mistral-vibe-cli-fast",
    "mistral-vibe-cli-latest",
    "mistral-vibe-cli-with-tools",
    "voxtral-small-2507",
    "voxtral-small-latest",
]

# Gemini embeddings, same key as the chat lineup. Added for OpenClaw memory search,
# whose index needs an embedding backend and had none — see
# docs/personal_agents/findings/harness_behaviour.md F9.
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
# generation: ai.google.dev/gemini-api/docs/models/gemini-3.7-flash and
# .../gemini-3.5-flash-lite (2026-08-23). Unlike
# Codex's CODEX_CONTEXT_WINDOW above, there is no live serving-path probe for
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
