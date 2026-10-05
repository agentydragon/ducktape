"""Account-specific model facts and canonical routes shared by configuration consumers.

Select Route objects and serialize their attributes at the consumer boundary.
Deployment endpoints and credentials belong to the consuming deployment, not this catalogue.

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
litellm/keys.py) enumerates one provider's names explicitly, so the lane is the common
prefix (and a LiteLLM prefix wildcard would carve the same lane). Deliberately not
renamed: the raw upstream model slugs inside the exposed names, and the two groq whisper
entries, whose historical bare IDs are retained even though their outbound shape is known. The bare
`gemini-embedding-2` alias (GEMINI_EMBEDDING_COMPAT_ALIAS) is exempt too: it predates the
scheme and public-coder-agent's durable memory index stores that model identity, so it
stays until the index is deliberately rebuilt.
"""

from dataclasses import dataclass
from enum import StrEnum

from model_catalog import ollama


class Provider(StrEnum):
    """First scheme segment: the upstream account/provider an entry spends from."""

    CHATGPT = "chatgpt"
    ANTHROPIC_API = "anthropic-api"
    ANTHROPIC_MAX20 = "anthropic-max20"
    ANTIGRAVITY = "antigravity"
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
    OAI_TRANSCRIBE = "oai-transcribe"


_SHAPE_MODE: dict[ApiShape, str] = {
    ApiShape.ANT_MESSAGES: "chat",
    ApiShape.OAI_CHAT: "chat",
    ApiShape.OAI_RESPONSES: "responses",
    ApiShape.GOOG_GENERATE: "chat",
    ApiShape.GOOG_EMBED: "embedding",
    ApiShape.OLM_CHAT: "chat",
    ApiShape.OLM_EMBED: "embedding",
    ApiShape.OAI_TRANSCRIBE: "audio_transcription",
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
    # LiteLLM's native Ollama chat adapter has its own provider prefix. It speaks
    # the same Ollama wire shape, so public `olm-chat` route names remain unchanged.
    "ollama_chat": "olm",
    # The in-process Tana adapter speaks Anthropic Messages on the wire while using its
    # own LiteLLM provider prefix for dispatch.
    "tana": "ant",
}


@dataclass(frozen=True)
class TokenLimits:
    """Declared input allowance and output ceiling for an account's serving path.

    Not a combined context window, nor a guarantee that both maxima are attainable
    together. Sources and uncertainty belong beside the declarations; this type does
    not turn a provisional allowance into a measured capacity. Client budgets and
    Ollama's num_ctx are separate settings, not derived from this pair.
    """

    max_input_tokens: int
    max_output_tokens: int


@dataclass(frozen=True)
class Model:
    """Metadata for a model as served by its account, not a universal vendor claim.

    Unknown facts stay unset. The comments at each declaration record whether limits
    are published, measured, or a conservative bound. Sharing this value between wires
    is deliberate; equal upstream slugs on different accounts do not imply equal limits.
    """

    id: str
    display_name: str | None = None
    limits: TokenLimits | None = None
    reasoning: bool | None = None


@dataclass(frozen=True)
class Upstream:
    """One account and outbound adapter; its shape is derived, not separately authored."""

    provider: Provider
    prefix: str
    protocol: str
    supports_function_calling: bool = False

    @property
    def shape(self) -> ApiShape:
        return ApiShape(f"{_UPSTREAM_DEFINER[self.prefix]}-{self.protocol}")


@dataclass(frozen=True)
class Route:
    """A served identity. Consumers select this object and serialize its attributes."""

    model: Model
    upstream: Upstream
    upstream_model: str | None = None
    reasoning_efforts: tuple[str, ...] = ()
    # Transitional: preserve existing LiteLLM publication while declarations are
    # completed. Remove this gate once every served route owns its token metadata.
    publish_limits: bool = False
    # Native Ollama request configuration, not evidence of attended context capacity.
    num_ctx: int | None = None
    # The two historical audio route IDs predate the naming scheme.
    bare_name: bool = False

    @property
    def id(self) -> str:
        if self.bare_name:
            return self.model.id
        return f"{self.upstream.provider}/{self.upstream.shape}/{self.model.id}"

    @property
    def display_name(self) -> str:
        if self.model.display_name is None:
            raise ValueError(f"no display name declared for offered route {self.id}")
        return self.model.display_name

    @property
    def upstream_id(self) -> str:
        return f"{self.upstream.prefix}/{self.upstream_model or self.model.id}"


@dataclass(frozen=True)
class RouteAlias:
    """An explicit compatibility identity referencing a canonical route."""

    id: str
    target: Route


_ANTHROPIC_EFFORTS = ("low", "medium", "high", "max")
_CODEX_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")

# ChatGPT/Codex-subscription models behind CLIProxyAPI, exposed on both wire surfaces
# for clients that need them. OpenClaw uses the Responses surface below because it is
# the working native passthrough to CLIProxyAPI.
#
# OpenAI API price reference, fetched 2026-09-24 from
# developers.openai.com/api/docs/pricing (USD per 1M tokens; standard, short-context
# rates; cached input / cache write / output):
# GPT-6 Sol and Luna were added to the Codex model catalog on 2026-09-22 (source:
# learn.chatgpt.com/docs/changelog), so they belong in this CLIProxyAPI-backed route
# roster even though their serving-path limits are not yet measured here.
#
# Standard short-context rates, listed as input / cached input / cache write / output:
#   gpt-6-astra:   $10 / $1 / $12.50 / $50
#   gpt-6-sol:      $2 / $0.20 / $2.50 / $10
#   gpt-6-luna:  $0.10 / $0.01 / $0.125 / $0.50
#   gpt-5.6-sol:    $4 / $0.40 / $5 / $20
#   gpt-5.6-terra:  $2 / $0.20 / $2.50 / $12
#   gpt-5.6-luna: $0.20 / $0.02 / $0.25 / $1.20
# GPT-6 long-context input/output rates are $20/$75, $4/$15, and $0.20/$0.75 for
# Astra/Sol/Luna; prompts over 272K are billed at those long-context rates for the
# full request. GPT-5.6 model pages state the same 2x-input/1.5x-output rule (so the
# implied long-context rates are $8/$30, $4/$18, and $0.40/$1.80). These are direct
# API prices, not measured subscription/CLIProxyAPI costs; GPT-5.6 Sol's promotional
# rate is documented through at least 2026-11-21. They are kept here as dated
# accounting reference only.


# Existing ChatGPT-subscription metadata allowances, NOT a verified capacity pair.
# GPT-5.6 input probing on 2026-07-29 accepted 370,629 tokens and rejected 372,194;
# it established neither an exact input maximum nor the 128K output ceiling. GPT-6
# Sol/Luna inherited this declaration, not independent serving-path measurements.
# Astra's 872K came from Codex 0.153.4's configurable client window, not an input
# capacity measurement. Preserve these already-published proxy values pending the
# source audit, without passing them to consumers as client budgets. See
# model_catalog/client_budgets.md; raw OpenAI API limits are not evidence for this path.
_CHATGPT_LIMITS = TokenLimits(max_input_tokens=372_000, max_output_tokens=128_000)
_ASTRA_LIMITS = TokenLimits(max_input_tokens=872_000, max_output_tokens=128_000)


GPT_6_ASTRA = Model(id="gpt-6-astra", display_name="GPT-6 Astra", limits=_ASTRA_LIMITS, reasoning=True)
GPT_6_LUNA = Model(id="gpt-6-luna", display_name="GPT-6 Luna", limits=_CHATGPT_LIMITS, reasoning=True)
GPT_6_SOL = Model(id="gpt-6-sol", display_name="GPT-6 Sol", limits=_CHATGPT_LIMITS, reasoning=True)
GPT_5_6_LUNA = Model(id="gpt-5.6-luna", display_name="GPT-5.6 Luna", limits=_CHATGPT_LIMITS, reasoning=True)
GPT_5_6_TERRA = Model(id="gpt-5.6-terra", display_name="GPT-5.6 Terra", limits=_CHATGPT_LIMITS, reasoning=True)
GPT_5_6_SOL = Model(id="gpt-5.6-sol", display_name="GPT-5.6 Sol", limits=_CHATGPT_LIMITS, reasoning=True)

# Tana-UI models served by the main LiteLLM proxy's in-process Tana provider. Tana
# encodes reasoning effort in the
# model name (`/medium`, `/high`), not a `reasoning_effort` param, so there is no clean
# "one model + effort knob" to map onto; we expose one model per family at its default
# effort. Each entry: (exposed-name base, Tana provider model suffix). The provider
# adds its dispatch prefix and preserves the suffix's slash for Tana.
TANA_MESSAGES = Upstream(Provider.TANA, "tana", "messages", supports_function_calling=True)
TANA_SONNET = Route(
    Model("claude-sonnet-4-6", "Claude Sonnet 4.6"), TANA_MESSAGES, upstream_model="tana/claude-sonnet-4-6/medium"
)
TANA_OPUS = Route(
    Model("claude-opus-4-6", "Claude Opus 4.6"), TANA_MESSAGES, upstream_model="tana/claude-opus-4-6/high"
)
TANA_HAIKU = Route(
    Model("claude-haiku-4-5", "Haiku 4.5"), TANA_MESSAGES, upstream_model="tana/claude-haiku-4-5-20251001"
)

# Current-generation Anthropic roster, verified against the authenticated /v1/models
# endpoint. Feeds Haku OpenClaw and the Terraform claude lane (litellm/keys.py), and is
# the exposed set for the cliproxyapi Claude-subscription `anthropic-max20/ant-messages/*` route: cliproxyapi's Claude
# OAuth session serves older generations too, but we expose only this current group — the
# subscription and the direct API serve the same current models, and sharing one list
# keeps them in sync ("newest group only", as with the Gemini roster).
_OPUS = Model("claude-opus-5", "Opus 5")
_SONNET = Model("claude-sonnet-5", "Sonnet 5")
_FABLE = Model("claude-fable-5", "Fable 5")
_HAIKU = Model("claude-haiku-4-5-20251001", "Haiku 4.5")


# Google's Antigravity OAuth session in CLIProxyAPI (agentydragon@gmail.com, added
# 2026-09-25) -- a personal-account "Google AI Plus" subscription, structurally
# unrelated to the AI-Studio GEMINI_API_KEY below: CLIProxyAPI's own AI Providers page
# carries zero configured API-key providers, so this OAuth session is its only
# Google-model credential. Verified against the vendored source
# (third_party/cli_proxy_api, github.com/router-for-me/CLIProxyAPI):
# `internal/runtime/executor/antigravity_executor.go` calls Google's internal Cloud Code
# API (cloudcode-pa.googleapis.com), not the public Gemini Developer API, and
# `internal/translator/antigravity/claude/` is a dedicated Anthropic-Messages
# translator for it -- the same wire mechanism already used for the chatgpt/
# anthropic-max20 routes below. The account bundles three unrelated model families
# under one weekly-refreshing quota (two buckets: "Gemini models" and "Claude and GPT
# models"; confirmed live via the management UI's quota refresh, 2026-09-25):
# non-current-generation Claude, Google's own Gemini lineup under Antigravity-specific
# slugs that don't match the public API names (reasoning-tier suffixes baked into the
# slug: -high/-low/-lite/-agent), and the open-weight (Apache-2.0) gpt-oss-120b, which
# Google can self-host like anyone else. Full catalog exposed as discovered; unlike
# the Anthropic and Gemini API rosters, there is no "current generation only" curation
# here yet.
#
# `reasoning` mirrors the slug's own baked-in effort tier (-high/-agent/-medium as
# True, -low/-lite/plain/-image as False). OpenClaw consumes this capability flag;
# the Agentplane projection separately uses the route's declared effort choices.
#
# Source mapping: Google's maxTokens -> TokenLimits.max_input_tokens;
# maxOutputTokens -> TokenLimits.max_output_tokens. For gemini-3.1-flash-lite, maxTokens
# is an INPUT allowance, not input+output: the live check crossed that combined
# total. Do not subtract the output allowance from it. Claude/GPT-OSS were not
# covered by that check; source/probe evidence is in model_catalog/antigravity_limits.md.
# The pinned snapshot below is historical; that document also records fresh Google
# metadata from 2026-10-05. Refreshing provider metadata must not change client budgets.
#
# The input/output declarations below are Google metadata for each model as
# served through Antigravity, not a public-API figure borrowed from Anthropic/OpenAI/a
# third-party host -- and deliberately not the result of a live binary-search probe
# (openai_utils/probe_context_window.py) run against claude-opus-4-6-thinking on
# 2026-09-26, which found requests up to ~575k tokens "accepted" with a
# correctly-echoed input_tokens count. That accept is real but its meaning is NOT
# settled: it shows the server didn't reject the oversized request, not that the model
# actually attended to all of it. Silent server-side truncation beyond the declared
# capacity (still reporting the full sent count for billing) is a plausible
# explanation and reads identically to a genuine accept, but it is unconfirmed --
# no experiment here distinguishes "really uses 575k" from "silently drops everything
# past ~200k." The declared figures below come from `third_party/cli_proxy_api`'s vendored CLIProxyAPI
# source (github.com/router-for-me/CLIProxyAPI, pinned commit 7fac6b15bcfe), which
# ships `cmd/fetch_antigravity_models` -- a tool that calls Google's own
# `/v1internal:fetchAvailableModels` endpoint (the same private Cloud Code API the live
# executor uses) with a real Antigravity OAuth token and records its `maxTokens`/
# `maxOutputTokens` fields verbatim into `internal/registry/models/models.json`'s
# `antigravity` section (checked 2026-09-26). `None` marks a model missing from that
# file entirely (gemini-3.5-flash-lite) or present with both fields null
# (gemini-3.1-flash-image, an image-output model) -- left for a follow-up.


_ANTIGRAVITY_OPUS = Model(
    id="claude-opus-4-6-thinking",
    display_name="Claude Opus 4.6 (Thinking)",
    reasoning=True,
    limits=TokenLimits(max_input_tokens=200_000, max_output_tokens=64_000),
)
_ANTIGRAVITY_SONNET = Model(
    id="claude-sonnet-4-6",
    display_name="Claude Sonnet 4.6 (Thinking)",
    reasoning=True,
    limits=TokenLimits(max_input_tokens=200_000, max_output_tokens=64_000),
)
_ANTIGRAVITY_FLASH_LITE_31 = Model(
    id="gemini-3.1-flash-lite",
    display_name="Gemini 3.1 Flash Lite",
    reasoning=False,
    limits=TokenLimits(max_input_tokens=1_048_576, max_output_tokens=65_535),
)
_ANTIGRAVITY_FLASH_LITE_35 = Model(id="gemini-3.5-flash-lite", display_name="Gemini 3.5 Flash Lite", reasoning=False)

_ANTIGRAVITY_PRO = Model(
    id="gemini-pro-agent",
    display_name="Gemini 3.1 Pro (High)",
    reasoning=True,
    limits=TokenLimits(max_input_tokens=1_048_576, max_output_tokens=65_535),
)


# Google AI (Gemini). Key from the GEMINI_API_KEY env var (litellm-gemini-key
# secret). Current-generation lineup only (Gemini 3.x) -- the 2.5 generation,
# the shut-down gemini-3-pro-preview, and every non-latest 3.x minor version
# (gemini-3-flash-preview, 3.5-flash, 3.6-flash, 3.1-flash-lite) are dropped,
# the same "only the newest group" policy as the Codex picker. The
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
# key (litellm/keys.py) and public-coder-agent's OpenClaw catalog.
# Published input/output token limits shared across the current Gemini chat
# generation: ai.google.dev/gemini-api/docs/models/gemini-3.7-flash and
# .../gemini-3.5-flash-lite (2026-08-23). Unlike
# the provisional ChatGPT allowances above, this pair comes from Google's
# published figures, not a serving-path probe. OpenClaw keeps its own budgets.
_GEMINI_LIMITS = TokenLimits(max_input_tokens=1_048_576, max_output_tokens=65_536)

_GEMINI_FLASH = Model(id="gemini-3.7-flash", display_name="Gemini 3.7 Flash", reasoning=True, limits=_GEMINI_LIMITS)
_GEMINI_FLASH_LITE = Model(
    id="gemini-3.5-flash-lite", display_name="Gemini 3.5 Flash-Lite", reasoning=False, limits=_GEMINI_LIMITS
)

# Mistral chat models that accepted a minimal completion with the cluster's API
# key on 2026-08-31. Catalog entries that returned 403 are intentionally
# excluded; account-specific fine-tuned models are excluded as well.
_MISTRAL_MODELS = (
    Model("codestral-2508"),
    Model("codestral-latest"),
    Model("magistral-medium-latest"),
    Model("magistral-small-latest"),
    Model("ministral-14b-latest"),
    Model("ministral-14b-2512"),
    Model("ministral-8b-latest"),
    Model("ministral-8b-2512"),
    Model("ministral-3b-latest"),
    Model("ministral-3b-2512"),
    Model("mistral-code-fim-latest"),
    Model("mistral-code-latest"),
    Model("mistral-medium"),
    Model("mistral-medium-2604"),
    Model("mistral-medium-3"),
    Model("mistral-medium-3-5"),
    Model("mistral-medium-3.5"),
    Model("mistral-medium-latest"),
    Model("mistral-small-2603"),
    Model("mistral-small-latest"),
    Model("mistral-vibe-cli-fast"),
    Model("mistral-vibe-cli-latest"),
    Model("mistral-vibe-cli-with-tools"),
    Model("voxtral-small-2507"),
    Model("voxtral-small-latest"),
)

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
_GEMINI_EMBEDDING_2 = Model("gemini-embedding-2")
_GEMINI_EMBEDDING_001 = Model("gemini-embedding-001")

# Bare, pre-scheme alias of GEMINI_EMBEDDING_2, served until the durable
# OpenClaw index public-coder-agent built under this identity is deliberately rebuilt
# under the prefixed name.
GEMINI_EMBEDDING_COMPAT_ALIAS = _GEMINI_EMBEDDING_2.id


# Canonical served routes. These are the only account/wire/model associations;
# downstream code receives Route objects, not naming ingredients.
OLLAMA_OPENAI = Upstream(Provider.OLLAMA, "openai", "chat", supports_function_calling=True)
OLLAMA_NATIVE = Upstream(Provider.OLLAMA, "ollama_chat", "chat", supports_function_calling=True)


@dataclass(frozen=True)
class OllamaRoutes:
    openai: Route
    native: Route


def _ollama_routes(variant: ollama.ChatVariant, *, openai_reasoning_efforts: tuple[str, ...] = ()) -> OllamaRoutes:
    context = variant.num_ctx
    suffix = "1M" if context == 1024 * 1024 else f"{context // 1024}K"
    model = Model(
        id=f"{variant.model.tag.removesuffix(':latest').replace(':', '-')}-{suffix.lower()}",
        display_name=f"{variant.model.display_name} ({suffix})",
    )
    return OllamaRoutes(
        openai=Route(model, OLLAMA_OPENAI, upstream_model=variant.tag, reasoning_efforts=openai_reasoning_efforts),
        native=Route(model, OLLAMA_NATIVE, upstream_model=variant.tag, num_ctx=context),
    )


# The efforts a client can ask Ollama for that Qwen3.8's chat template
# (cluster/cdk8s/ollama/qwen38-chat-template.jinja) accepts. The template takes xhigh (its
# default), medium and low, and treats `high` as xhigh. Ollama rewrites a requested `xhigh`
# to `max` unless the model has thinking metadata, which a GGUF registered through
# /api/create lacks, and the template rejects `max`: `high` is how a client asks for
# Qwen's xhigh. Declared on the OpenAI-compatible wire only: on the native wire LiteLLM's
# mapper fails on the reasoning object Codex sends.
_QWEN_EFFORTS = ("low", "medium", "high")

OLLAMA_QWEN_IQ4XS_128K = _ollama_routes(ollama.QWEN_IQ4XS_128K, openai_reasoning_efforts=_QWEN_EFFORTS)
OLLAMA_QWEN_IQ4XS_256K = _ollama_routes(ollama.QWEN_IQ4XS_256K, openai_reasoning_efforts=_QWEN_EFFORTS)
_GPT_OSS_20B_128K = _ollama_routes(ollama.ChatVariant(ollama.GPT_OSS_20B, 128 * 1024))
OLLAMA_GPT_OSS_20B_128K = _GPT_OSS_20B_128K.openai
_OLLAMA_ROUTE_GROUPS = (
    (OLLAMA_QWEN_IQ4XS_128K,),
    (OLLAMA_QWEN_IQ4XS_256K,),
    # Existing request-only variants: no corresponding baked GPT-OSS aliases.
    (
        _GPT_OSS_20B_128K,
        *(_ollama_routes(ollama.ChatVariant(ollama.GPT_OSS_20B, context * 1024)) for context in (256, 512, 1024)),
    ),
    (_ollama_routes(ollama.ChatVariant(ollama.GPT_OSS_120B, 128 * 1024)),),
    (_ollama_routes(ollama.ChatVariant(ollama.GEMMA4, 128 * 1024)),),
)
OLLAMA_OPENAI_ROUTES = tuple(pair.openai for group in _OLLAMA_ROUTE_GROUPS for pair in group)
OLLAMA_CHAT_ROUTES = tuple(
    route for group in _OLLAMA_ROUTE_GROUPS for pair in group for route in (pair.openai, pair.native)
)
# The proxy historically groups wires within each source; keys interleave wires per
# context. Preserve both output orders while referencing exactly the same objects.
_OLLAMA_PROXY_ROUTES = tuple(
    route
    for group in _OLLAMA_ROUTE_GROUPS
    for route in (*(pair.openai for pair in group), *(pair.native for pair in group))
)
OLLAMA_EMBED = Upstream(Provider.OLLAMA, "ollama", "embed")
OLLAMA_EMBEDDING_ROUTE = Route(
    Model(ollama.QWEN_EMBEDDING.tag.replace(":", "-")), OLLAMA_EMBED, upstream_model=ollama.QWEN_EMBEDDING.tag
)
TANA_ROUTES = (TANA_SONNET, TANA_OPUS, TANA_HAIKU)
CHATGPT_MESSAGES = Upstream(Provider.CHATGPT, "anthropic", "messages", supports_function_calling=True)
CHATGPT_RESPONSES = Upstream(Provider.CHATGPT, "openai", "responses", supports_function_calling=True)
GPT6_ASTRA_MESSAGES = Route(GPT_6_ASTRA, CHATGPT_MESSAGES, publish_limits=True)
GPT6_LUNA_MESSAGES = Route(GPT_6_LUNA, CHATGPT_MESSAGES, publish_limits=True)
GPT6_SOL_MESSAGES = Route(GPT_6_SOL, CHATGPT_MESSAGES, publish_limits=True)
GPT6_MESSAGES_ROUTES = (GPT6_ASTRA_MESSAGES, GPT6_LUNA_MESSAGES, GPT6_SOL_MESSAGES)
CHATGPT_MESSAGES_ROUTES = (
    GPT6_ASTRA_MESSAGES,
    GPT6_SOL_MESSAGES,
    GPT6_LUNA_MESSAGES,
    *(
        Route(model, CHATGPT_MESSAGES, publish_limits=model.limits is not None)
        for model in (GPT_5_6_SOL, GPT_5_6_TERRA, GPT_5_6_LUNA)
    ),
)
GPT6_ASTRA_RESPONSES = Route(GPT_6_ASTRA, CHATGPT_RESPONSES, publish_limits=True, reasoning_efforts=_CODEX_EFFORTS)
GPT6_LUNA_RESPONSES = Route(GPT_6_LUNA, CHATGPT_RESPONSES, publish_limits=True, reasoning_efforts=_CODEX_EFFORTS)
GPT6_SOL_RESPONSES = Route(GPT_6_SOL, CHATGPT_RESPONSES, publish_limits=True, reasoning_efforts=_CODEX_EFFORTS)
GPT6_RESPONSES_ROUTES = (GPT6_ASTRA_RESPONSES, GPT6_LUNA_RESPONSES, GPT6_SOL_RESPONSES)
CHATGPT_RESPONSES_ROUTES = (
    GPT6_ASTRA_RESPONSES,
    GPT6_SOL_RESPONSES,
    GPT6_LUNA_RESPONSES,
    *(
        Route(model, CHATGPT_RESPONSES, publish_limits=model.limits is not None, reasoning_efforts=_CODEX_EFFORTS)
        for model in (GPT_5_6_SOL, GPT_5_6_TERRA, GPT_5_6_LUNA)
    ),
)
ANTHROPIC_SUBSCRIPTION = Upstream(Provider.ANTHROPIC_MAX20, "anthropic", "messages", supports_function_calling=True)
OPUS_SUBSCRIPTION = Route(_OPUS, ANTHROPIC_SUBSCRIPTION, reasoning_efforts=_ANTHROPIC_EFFORTS)
SONNET_SUBSCRIPTION = Route(_SONNET, ANTHROPIC_SUBSCRIPTION, reasoning_efforts=_ANTHROPIC_EFFORTS)
FABLE_SUBSCRIPTION = Route(_FABLE, ANTHROPIC_SUBSCRIPTION, reasoning_efforts=_ANTHROPIC_EFFORTS)
HAIKU_SUBSCRIPTION = Route(_HAIKU, ANTHROPIC_SUBSCRIPTION, reasoning_efforts=_ANTHROPIC_EFFORTS)
ANTHROPIC_SUBSCRIPTION_ROUTES = (OPUS_SUBSCRIPTION, SONNET_SUBSCRIPTION, FABLE_SUBSCRIPTION, HAIKU_SUBSCRIPTION)
ANTHROPIC_API = Upstream(Provider.ANTHROPIC_API, "anthropic", "messages", supports_function_calling=True)
OPUS_API = Route(_OPUS, ANTHROPIC_API)
SONNET_API = Route(_SONNET, ANTHROPIC_API)
FABLE_API = Route(_FABLE, ANTHROPIC_API)
HAIKU_API = Route(_HAIKU, ANTHROPIC_API)
ANTHROPIC_API_ROUTES = (OPUS_API, SONNET_API, FABLE_API, HAIKU_API)
ANTIGRAVITY_MESSAGES = Upstream(Provider.ANTIGRAVITY, "anthropic", "messages", supports_function_calling=True)
ANTIGRAVITY_OPUS = Route(_ANTIGRAVITY_OPUS, ANTIGRAVITY_MESSAGES, reasoning_efforts=_ANTHROPIC_EFFORTS)
ANTIGRAVITY_SONNET = Route(_ANTIGRAVITY_SONNET, ANTIGRAVITY_MESSAGES, reasoning_efforts=_ANTHROPIC_EFFORTS)
ANTIGRAVITY_PRO = Route(_ANTIGRAVITY_PRO, ANTIGRAVITY_MESSAGES)
ANTIGRAVITY_FLASH_LITE_31 = Route(_ANTIGRAVITY_FLASH_LITE_31, ANTIGRAVITY_MESSAGES)
ANTIGRAVITY_FLASH_LITE = Route(_ANTIGRAVITY_FLASH_LITE_35, ANTIGRAVITY_MESSAGES)
ANTIGRAVITY_FLASH_36 = Route(
    Model(
        id="gemini-3.6-flash-high",
        display_name="Gemini 3.6 Flash",
        reasoning=True,
        limits=TokenLimits(max_input_tokens=1_048_576, max_output_tokens=65_536),
    ),
    ANTIGRAVITY_MESSAGES,
)
ANTIGRAVITY_FLASH_37 = Route(
    Model(
        id="gemini-3.7-flash-high",
        display_name="Gemini 3.7 Flash",
        reasoning=True,
        limits=TokenLimits(max_input_tokens=1_048_576, max_output_tokens=65_536),
    ),
    ANTIGRAVITY_MESSAGES,
)
ANTIGRAVITY_FLASH_38 = Route(
    Model(
        id="gemini-3.8-flash-high",
        display_name="Gemini 3.8 Flash",
        reasoning=True,
        limits=TokenLimits(max_input_tokens=1_048_576, max_output_tokens=65_536),
    ),
    ANTIGRAVITY_MESSAGES,
)
ANTIGRAVITY_FLASH_3 = Route(
    Model(
        id="gemini-3-flash",
        display_name="Gemini 3 Flash",
        reasoning=False,
        limits=TokenLimits(max_input_tokens=1_048_576, max_output_tokens=65_536),
    ),
    ANTIGRAVITY_MESSAGES,
)
ANTIGRAVITY_FLASH_IMAGE = Route(
    Model(id="gemini-3.1-flash-image", display_name="Gemini 3.1 Flash Image", reasoning=False), ANTIGRAVITY_MESSAGES
)
ANTIGRAVITY_PRO_LOW = Route(
    Model(
        id="gemini-3.1-pro-low",
        display_name="Gemini 3.1 Pro (Low)",
        reasoning=False,
        limits=TokenLimits(max_input_tokens=1_048_576, max_output_tokens=65_535),
    ),
    ANTIGRAVITY_MESSAGES,
)
ANTIGRAVITY_GPT_OSS_120B_MEDIUM = Route(
    Model(
        id="gpt-oss-120b-medium",
        display_name="GPT-OSS 120B (Medium)",
        reasoning=True,
        limits=TokenLimits(max_input_tokens=114_000, max_output_tokens=32_768),
    ),
    ANTIGRAVITY_MESSAGES,
)
ANTIGRAVITY_ROUTES = (
    ANTIGRAVITY_OPUS,
    ANTIGRAVITY_SONNET,
    ANTIGRAVITY_FLASH_36,
    ANTIGRAVITY_FLASH_37,
    ANTIGRAVITY_FLASH_38,
    ANTIGRAVITY_FLASH_3,
    ANTIGRAVITY_FLASH_IMAGE,
    ANTIGRAVITY_PRO,
    ANTIGRAVITY_PRO_LOW,
    ANTIGRAVITY_GPT_OSS_120B_MEDIUM,
    ANTIGRAVITY_FLASH_LITE_31,
    ANTIGRAVITY_FLASH_LITE,
)
ANTIGRAVITY_FLASH_LITE_ROUTES = (ANTIGRAVITY_FLASH_LITE_31, ANTIGRAVITY_FLASH_LITE)
GROQ_CHAT = Upstream(Provider.GROQ, "groq", "chat", supports_function_calling=True)
GROQ_CHAT_ROUTES = tuple(Route(Model(id), GROQ_CHAT) for id in ("llama-3.3-70b-versatile", "llama-3.1-8b-instant"))
GROQ_TRANSCRIBE = Upstream(Provider.GROQ, "groq", "transcribe")
GROQ_AUDIO_ROUTES = tuple(
    Route(Model(id), GROQ_TRANSCRIBE, bare_name=True) for id in ("whisper-large-v3", "whisper-large-v3-turbo")
)
GOOGLE_GENERATE = Upstream(Provider.GOOGLE, "gemini", "generate", supports_function_calling=True)
GEMINI_FLASH = Route(_GEMINI_FLASH, GOOGLE_GENERATE)
GEMINI_FLASH_LITE = Route(_GEMINI_FLASH_LITE, GOOGLE_GENERATE)
GEMINI_ROUTES = (GEMINI_FLASH, GEMINI_FLASH_LITE)
GOOGLE_EMBED = Upstream(Provider.GOOGLE, "gemini", "embed")
GEMINI_EMBEDDING_2 = Route(_GEMINI_EMBEDDING_2, GOOGLE_EMBED)
GEMINI_EMBEDDING_001 = Route(_GEMINI_EMBEDDING_001, GOOGLE_EMBED)
GEMINI_EMBEDDING_ROUTES = (GEMINI_EMBEDDING_2, GEMINI_EMBEDDING_001)
GEMINI_EMBEDDING_ALIAS = RouteAlias(GEMINI_EMBEDDING_COMPAT_ALIAS, GEMINI_EMBEDDING_2)
MISTRAL_CHAT = Upstream(Provider.MISTRAL, "mistral", "chat", supports_function_calling=True)
MISTRAL_ROUTES = tuple(Route(model, MISTRAL_CHAT) for model in _MISTRAL_MODELS)

# Ordered public catalog. Aliases reference routes rather than repeat their upstream
# or metadata. Hidden harness-compatibility aliases are not advertised as model entries.
SERVED_ROUTES: tuple[Route | RouteAlias, ...] = (
    *_OLLAMA_PROXY_ROUTES,
    OLLAMA_EMBEDDING_ROUTE,
    *TANA_ROUTES,
    *CHATGPT_MESSAGES_ROUTES,
    *CHATGPT_RESPONSES_ROUTES,
    *ANTHROPIC_SUBSCRIPTION_ROUTES,
    *ANTHROPIC_API_ROUTES,
    *ANTIGRAVITY_ROUTES,
    *GROQ_CHAT_ROUTES,
    *GROQ_AUDIO_ROUTES,
    *GEMINI_ROUTES,
    GEMINI_EMBEDDING_2,
    GEMINI_EMBEDDING_ALIAS,
    GEMINI_EMBEDDING_001,
    *MISTRAL_ROUTES,
)
HIDDEN_ALIASES = (RouteAlias(GPT6_ASTRA_RESPONSES.model.id, GPT6_ASTRA_RESPONSES),)
