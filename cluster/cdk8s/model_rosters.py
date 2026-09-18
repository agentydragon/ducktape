"""Shared model rosters and exposed-name derivations for cdk8s-generated LiteLLM and agent configs.

Naming scheme (#4823): an exposed `model_name` is `{provider}/{shape}/{model}` — the
upstream account/provider, the wire LiteLLM speaks to that provider, then the upstream
model:

- `chatgpt/ant-messages/*` / `chatgpt/oai-responses/*` — ChatGPT/Codex subscription via
  CLIProxyAPI, which serves it on both the Anthropic Messages and the OpenAI Responses
  wire; the two entries pick between them
- `anthropic-max20/ant-messages/*` — Claude Code subscription via CLIProxyAPI's Claude
  OAuth session, on the Anthropic Messages wire (a different upstream session on the same
  pod as `chatgpt/*`, distinct from the direct-API `anthropic-api/ant-messages/*` entries)
- `anthropic-api/ant-messages/*` — direct Anthropic API on the Anthropic Messages wire
- `tana/ant-messages/*` — Tana account via tana-litellm, an Anthropic Messages
  passthrough
- `google/goog-generate/*` / `google/goog-embed/*` — Google AI key (Gemini), on Google's
  own `:generateContent` / `:embedContent` wire
- `mistral/oai-chat/*` — Mistral API key
- `groq/oai-chat/*` — Groq API key, OpenAI-compatible at api.groq.com/openai/v1
- `ollama/oai-chat/*` / `ollama/olm-chat/*` / `ollama/olm-embed/*` — self-hosted Ollama,
  which serves the same models on an OpenAI-compatible `/v1` and on its own native
  endpoints; the chat model segment carries the `num_ctx` variant (`gpt-oss-20b-512k`)
  that distinguishes chat entries

The shape is the OUTBOUND wire — the request LiteLLM makes to the provider, never the
request a client makes to LiteLLM. Nothing about the inbound side is pinned: LiteLLM routes
on `model_name` alone, takes the incoming shape from whichever endpoint the client called,
and translates rather than rejecting a mismatch, so every entry is reachable from
`/v1/messages`, `/v1/chat/completions` and `/v1/responses` alike — the laptop
`gemini-claude` wrapper runs Claude Code's `/v1/messages` traffic against
`google/goog-generate/*`. What the shape does decide is which translator does the work,
load-bearing wherever one account is served over two wires:
`chatgpt/{ant-messages,oai-responses}/*` exists so CLIProxyAPI translates Claude Code's
tool calls instead of LiteLLM's own Messages bridge (<nix/home/claude_code/codex-claude.nix>).

A shape slug is `<definer>-<protocol>` (ant-messages, oai-responses, oai-chat,
goog-generate, goog-embed, olm-chat): the shape segment names a wire protocol, and wire protocols are
identified by their definer — the bare nouns are unique only in today's snapshot
("chat" and "embeddings" are already generic: Cohere chat and Google embedContent are
distinct wire shapes answering to the same nouns). The definer prefix is NOT the
provider segment: provider says whose ACCOUNT serves the entry, the definer says whose
PROTOCOL that wire is, and they vary independently — `chatgpt/ant-messages/*` is the
ChatGPT account serving Anthropic's wire shape. Definer slugs stay short and fixed
(ant, oai, goog, olm; future coh, ...) so a provider slug can never stutter against a
definer name (openai/openai-chat, ollama/ollama-chat).

Segments are separated by `/`, not `-`: provider model slugs are dash-heavy
(gpt-5.6-sol, claude-sonnet-4-6, gemini-embedding-001), so a dash cannot mark segment
boundaries unambiguously. `/` is LiteLLM's own model-group idiom (its docs' recommended
`model_name: openai/gpt-4o`, wildcard `openai/*`) and is already served in-cluster by
tana-litellm (`claude-sonnet-4-6/medium`, `gpt-5.1/medium`); clients carry the model in
the request body (Claude Code, Codex, OpenClaw), and on this stack a model name never
rides in a URL path or a Kubernetes resource name.

The provider segment rides in front, not behind, because a key allowlist is a provider
lane: each `litellm_key.models` list in tf/gitops/litellm-keys/main.tf (exported from
litellm_keys.py) enumerates one provider's names explicitly, so the lane is the common
prefix (and a LiteLLM prefix wildcard would carve the same lane). Deliberately not
renamed: the raw upstream model slugs inside the exposed names, and the two groq whisper
entries, whose `audio_transcription` mode no shape slug covers yet. The bare
`gemini-embedding-2` alias (GEMINI_EMBEDDING_COMPAT_ALIAS) is exempt too: it predates the
scheme and public-coder-agent's durable memory index stores that model identity, so it
stays until the index is deliberately rebuilt.
"""

from dataclasses import dataclass
from enum import StrEnum


class Provider(StrEnum):
    """First scheme segment: the upstream account/provider an entry spends from."""

    CHATGPT = "chatgpt"
    ANTHROPIC_API = "anthropic-api"
    ANTHROPIC_MAX20 = "anthropic-max20"
    TANA = "tana"
    GOOGLE = "google"
    MISTRAL = "mistral"
    OLLAMA = "ollama"
    GROQ = "groq"


class ApiShape(StrEnum):
    """Second scheme segment: the wire LiteLLM speaks upstream for the entry, as `<definer>-<protocol>`."""

    ANT_MESSAGES = "ant-messages"
    OAI_RESPONSES = "oai-responses"
    OAI_CHAT = "oai-chat"
    GOOG_GENERATE = "goog-generate"
    GOOG_EMBED = "goog-embed"
    OLM_CHAT = "olm-chat"
    OLM_EMBED = "olm-embed"


def exposed_name(provider: Provider, shape: ApiShape, model: str) -> str:
    """#4823 scheme name, e.g. `chatgpt/oai-responses/gpt-5.6-luna`."""
    return f"{provider}/{shape}/{model}"


def codex_responses_name(model: str) -> str:
    """A Codex-subscription model as served on LiteLLM's Responses surface -- the route
    Codex CLI clients and OpenClaw call."""
    return exposed_name(Provider.CHATGPT, ApiShape.OAI_RESPONSES, model)


def codex_messages_name(model: str) -> str:
    """A Codex-subscription model as served on the Anthropic Messages surface -- the route
    Claude Code clients call."""
    return exposed_name(Provider.CHATGPT, ApiShape.ANT_MESSAGES, model)


_SHAPE_MODE: dict[ApiShape, str] = {
    ApiShape.ANT_MESSAGES: "chat",
    ApiShape.OAI_CHAT: "chat",
    ApiShape.OAI_RESPONSES: "responses",
    ApiShape.GOOG_GENERATE: "chat",
    ApiShape.GOOG_EMBED: "embedding",
    ApiShape.OLM_CHAT: "chat",
    ApiShape.OLM_EMBED: "embedding",
}


def shape_mode(shape: ApiShape) -> str:
    """The LiteLLM `model_info.mode` this wire shape always implies."""
    return _SHAPE_MODE[shape]


# The upstream account/API family that speaks each definer -- several upstream prefixes
# can share one definer (openai/mistral/groq all speak "oai"), so a shape's definer
# can't be derived from the shape alone; it has to come from here.
_UPSTREAM_DEFINER: dict[str, str] = {
    "anthropic": "ant",
    "openai": "oai",
    "mistral": "oai",  # OpenAI-compatible chat at api.mistral.ai
    "groq": "oai",  # OpenAI-compatible chat at api.groq.com/openai/v1
    "gemini": "goog",
    "ollama": "olm",
    # The in-process Tana adapter speaks Anthropic Messages on the wire while using its
    # own LiteLLM provider prefix for dispatch.
    "tana": "ant",
}


def shape_for(upstream_prefix: str, protocol: str) -> ApiShape:
    """The wire shape for this upstream account and protocol.

    Deriving the shape this way -- rather than a call site naming `shape` and
    `upstream_prefix` as two independent values -- makes it structurally impossible to
    pick a shape whose definer disagrees with the upstream it's actually calling.
    """
    return ApiShape(f"{_UPSTREAM_DEFINER[upstream_prefix]}-{protocol}")


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


@dataclass(frozen=True)
class CodexModel:
    """A Codex-subscription model whose serving-path limits are known."""

    id: str
    display_name: str
    context_window: int
    max_tokens: int


# The Codex models with known serving-path limits: Astra from Codex's bundled metadata,
# the 5.6 models measured (CODEX_CONTEXT_WINDOW above). The LiteLLM manifest advertises
# these limits in model_info, and OpenClaw's model picker exposes exactly this subset,
# declaring the limits itself because its bundled LiteLLM provider does not query the
# proxy's authenticated /v1/models endpoint. gpt-5.4/5.5/5.3-codex-spark were never
# probed and stay out; a newly added 5.6 model must be probed before joining.
OPENCLAW_CODEX_MODELS: tuple[CodexModel, ...] = (
    CodexModel(
        id="gpt-6-astra", display_name="GPT-6 Astra", context_window=ASTRA_CONTEXT_WINDOW, max_tokens=ASTRA_MAX_TOKENS
    ),
    CodexModel(
        id="gpt-5.6-luna", display_name="GPT-5.6 Luna", context_window=CODEX_CONTEXT_WINDOW, max_tokens=CODEX_MAX_TOKENS
    ),
    CodexModel(
        id="gpt-5.6-terra",
        display_name="GPT-5.6 Terra",
        context_window=CODEX_CONTEXT_WINDOW,
        max_tokens=CODEX_MAX_TOKENS,
    ),
    CodexModel(
        id="gpt-5.6-sol", display_name="GPT-5.6 Sol", context_window=CODEX_CONTEXT_WINDOW, max_tokens=CODEX_MAX_TOKENS
    ),
)

# Tana-UI models served by the main LiteLLM proxy's in-process Tana provider. Tana
# encodes reasoning effort in the
# model name (`/medium`, `/high`), not a `reasoning_effort` param, so there is no clean
# "one model + effort knob" to map onto; we expose one model per family at its default
# effort. Each entry: (exposed-name base, Tana provider model suffix). The provider
# adds its dispatch prefix and preserves the suffix's slash for Tana.
TANA_MODELS: list[tuple[str, str]] = [
    ("claude-sonnet-4-6", "claude-sonnet-4-6/medium"),
    ("claude-opus-4-6", "claude-opus-4-6/high"),
    ("claude-haiku-4-5", "claude-haiku-4-5-20251001"),
]

# Current-generation Anthropic roster, verified against the authenticated /v1/models
# endpoint. Feeds Haku OpenClaw and the Terraform claude lane (litellm_keys.py), and is
# the exposed set for the cliproxyapi Claude-subscription `anthropic-max20/ant-messages/*` route: cliproxyapi's Claude
# OAuth session serves older generations too, but we expose only this current group — the
# subscription and the direct API serve the same current models, and sharing one list
# keeps them in sync ("newest group only", as with the Gemini roster).
ANTHROPIC_MODELS: list[str] = ["claude-opus-5", "claude-sonnet-5", "claude-fable-5", "claude-haiku-4-5-20251001"]


@dataclass(frozen=True)
class GeminiModel:
    id: str
    display_name: str
    # Capability/product positioning, not the literal default toggle: the "-lite" tier
    # is the deliberately cheap/fast one, while plain Flash is positioned around its
    # reasoning ("thinking") capability -- Google's gemini-3.7-flash page notes thinking
    # is supported but not automatic-by-default.
    reasoning: bool


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
# out of the roster until that is verified. Feeds the gemini-clients Terraform
# key (litellm_keys.py) and public-coder-agent's OpenClaw catalog.
GEMINI_MODELS: tuple[GeminiModel, ...] = (
    GeminiModel(id="gemini-3.7-flash", display_name="Gemini 3.7 Flash", reasoning=True),
    GeminiModel(id="gemini-3.5-flash-lite", display_name="Gemini 3.5 Flash-Lite", reasoning=False),
)

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

# Bare, pre-scheme alias of GEMINI_EMBEDDING_MODELS[0], served until the durable
# OpenClaw index public-coder-agent built under this identity is deliberately rebuilt
# under the prefixed name.
GEMINI_EMBEDDING_COMPAT_ALIAS = "gemini-embedding-2"

# Published input/output token limits shared across the current Gemini chat
# generation: ai.google.dev/gemini-api/docs/models/gemini-3.7-flash and
# .../gemini-3.5-flash-lite (2026-08-23). Unlike
# Codex's CODEX_CONTEXT_WINDOW above, there is no live serving-path probe for
# a third-party hosted API, so this is Google's published figure rather than
# a measured one. Used by public-coder-agent's OpenClaw catalog.
GEMINI_CONTEXT_WINDOW = 1_048_576
GEMINI_MAX_OUTPUT_TOKENS = 65_536

# Self-hosted Ollama chat models: (exposed model, Ollama model, num_ctx variants).
# Each variant is served on both the OpenAI-compatible `/v1` and Ollama's native
# wire; the context rides in the model segment (`ollama_chat_variant`) so the chat
# entries stay distinct.
OLLAMA_CHAT_MODELS: list[tuple[str, str, tuple[int, ...]]] = [
    ("gpt-oss-20b", "gpt-oss:20b", (128 * 1024, 256 * 1024, 512 * 1024, 1024 * 1024)),
    ("gpt-oss-120b", "gpt-oss:120b", (128 * 1024,)),
    ("gemma4-31b-it-q8_0", "gemma4:31b-it-q8_0", (128 * 1024,)),
]


def ollama_chat_variant(model: str, context: int) -> str:
    """The model segment of an Ollama chat entry at this `num_ctx`: `gpt-oss-20b-512k`."""
    return f"{model}-1m" if context == 1024 * 1024 else f"{model}-{context // 1024}k"


# The self-hosted Ollama embedding route (litellm_config.py's `_ollama_entries()`),
# also referenced by public-coder-agent's OpenClaw memory-search config so its
# embedding backend names the same route it's actually served on.
OLLAMA_EMBEDDING_MODEL = "qwen3-embedding-4b"
