"""The per-key model allowlists tf/gitops/litellm-keys/main.tf scopes its LiteLLM virtual
keys to, derived from model_rosters.py and handed to the module as the `model_allowlists`
variable of its generated Terraform CR (generate_manifests.py). Every name is checked
against what the main proxy serves before it is written.
"""

from __future__ import annotations

from cluster.cdk8s.litellm_config import main_proxy_config
from cluster.cdk8s.model_rosters import (
    ANTHROPIC_MODELS,
    CLIPROXY_MODELS,
    GEMINI_EMBEDDING_COMPAT_ALIAS,
    GEMINI_EMBEDDING_MODELS,
    GEMINI_MODELS,
    MISTRAL_MODELS,
    OLLAMA_CHAT_MODELS,
    OLLAMA_EMBEDDING_MODEL,
    TANA_MODELS,
    ApiShape,
    Provider,
    codex_messages_name,
    codex_responses_name,
    exposed_name,
    ollama_chat_variant,
)

# The Codex-subscription models on LiteLLM's Responses surface, for Codex CLI clients
# (codex-pod, agent-workspaces-codex, the agentplane staging session form) -- served
# by CLIProxyAPI.
OAI_LANE_MODELS = [codex_responses_name(model) for model in CLIPROXY_MODELS]
# The same models on the Anthropic Messages surface -- Claude Code clients
# (laptop codex-claude, agent-box, codex-pod).
CODEX_CLIENT_MODELS = [codex_messages_name(model) for model in CLIPROXY_MODELS]
# Claude-subscription models on the Anthropic Messages surface, fronted through
# CLIProxyAPI's Claude OAuth session -- the Console-launched Claude runner, the laptop
# litellm-claude wrapper, and the agentplane staging session form. A different
# upstream session on the same pod as the Codex lanes; distinct from the direct-API
# anthropic-api/ant-messages/* entries.
CLAUDE_CLIENT_MODELS = [
    exposed_name(Provider.ANTHROPIC_MAX20, ApiShape.ANT_MESSAGES, model) for model in ANTHROPIC_MODELS
]
# Tana-UI models served by the main proxy's in-process Tana provider -- the laptop
# tana-claude wrapper.
TANA_CLIENT_MODELS = [exposed_name(Provider.TANA, ApiShape.ANT_MESSAGES, exposed) for exposed, _ in TANA_MODELS]
# The Gemini chat lineup -- the laptop gemini-claude wrapper and public-coder-agent.
GEMINI_CLIENT_MODELS = [exposed_name(Provider.GOOGLE, ApiShape.GOOG_GENERATE, model.id) for model in GEMINI_MODELS]
_GEMINI_EMBEDDING_ROUTES = [
    exposed_name(Provider.GOOGLE, ApiShape.GOOG_EMBED, model) for model in GEMINI_EMBEDDING_MODELS
]
# Embeddings for agents whose egress cannot reach api.openai.com: a domain-confined
# agent has no route to a direct OpenAI Platform key and should not gain one just to
# embed, so its OpenClaw memory index rides the in-cluster path it already uses for
# turns.
EMBEDDING_CLIENT_MODELS = [
    GEMINI_EMBEDDING_COMPAT_ALIAS,
    *_GEMINI_EMBEDDING_ROUTES,
    exposed_name(Provider.OLLAMA, ApiShape.OLM_EMBED, OLLAMA_EMBEDDING_MODEL),
]

# The one native subscription model per harness the agentplane testing session form
# offers; the Codex one on both wires, for Claude Code clients on the same key.
CHEAP_EXPERIMENTS_CLAUDE_MODEL = exposed_name(
    Provider.ANTHROPIC_API, ApiShape.ANT_MESSAGES, "claude-haiku-4-5-20251001"
)
_CHEAP_EXPERIMENTS_CODEX = "gpt-5.6-luna"
CHEAP_EXPERIMENTS_CODEX_MODEL = codex_responses_name(_CHEAP_EXPERIMENTS_CODEX)
# The cheap-experiments key, shared with agents only through an expiring Haku Console
# Kubernetes grant and standing on the agentplane testing LLM ingress. Intentionally an
# exact, cheap-model-only set rather than a provider-wide prefix or wildcard: the Gemini
# chat and embedding lineups, the API-key-verified Mistral chat roster, every
# model/context/protocol variant of the self-hosted Ollama chat models, and the two
# native subscription models above.
CHEAP_EXPERIMENTS_MODELS = [
    *GEMINI_CLIENT_MODELS,
    *_GEMINI_EMBEDDING_ROUTES,
    *(exposed_name(Provider.MISTRAL, ApiShape.OAI_CHAT, model) for model in MISTRAL_MODELS),
    *(
        exposed_name(Provider.OLLAMA, shape, ollama_chat_variant(model, context))
        for model, _, contexts in OLLAMA_CHAT_MODELS
        for context in contexts
        for shape in (ApiShape.OAI_CHAT, ApiShape.OLM_CHAT)
    ),
    CHEAP_EXPERIMENTS_CLAUDE_MODEL,
    codex_messages_name(_CHEAP_EXPERIMENTS_CODEX),
    CHEAP_EXPERIMENTS_CODEX_MODEL,
]


def model_allowlists() -> dict[str, list[str]]:
    """The lanes keyed as main.tf's `var.model_allowlists` reads them."""
    served = {entry["model_name"] for entry in main_proxy_config()["model_list"]}
    lanes = {
        "oai_lane_models": OAI_LANE_MODELS,
        "tana_client_models": TANA_CLIENT_MODELS,
        "codex_client_models": CODEX_CLIENT_MODELS,
        "claude_client_models": CLAUDE_CLIENT_MODELS,
        "embedding_client_models": EMBEDDING_CLIENT_MODELS,
        "gemini_client_models": GEMINI_CLIENT_MODELS,
        "cheap_experiments_models": CHEAP_EXPERIMENTS_MODELS,
    }
    for lane, models in lanes.items():
        unserved = [model for model in models if model not in served]
        if unserved:
            raise ValueError(f"{lane=} allowlists models the proxy does not serve: {unserved}")
    return lanes
