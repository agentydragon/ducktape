"""Explicit consumer policy over canonical routes; no model facts or naming logic."""

from dataclasses import dataclass

from model_catalog.catalog import (
    ANTHROPIC_SUBSCRIPTION_ROUTES,
    ANTIGRAVITY_FLASH_LITE,
    ANTIGRAVITY_FLASH_LITE_ROUTES,
    ANTIGRAVITY_ROUTES,
    CHATGPT_MESSAGES_ROUTES,
    GEMINI_EMBEDDING_ALIAS,
    GEMINI_EMBEDDING_ROUTES,
    GEMINI_FLASH_LITE,
    GEMINI_ROUTES,
    GPT6_LUNA_MESSAGES,
    GPT6_LUNA_RESPONSES,
    GPT6_MESSAGES_ROUTES,
    GPT6_RESPONSES_ROUTES,
    HAIKU_API,
    MISTRAL_ROUTES,
    OLLAMA_CHAT_ROUTES,
    OLLAMA_EMBEDDING_ROUTE,
    OLLAMA_QWEN_IQ4XS_128K,
    OLLAMA_QWEN_IQ4XS_256K,
    RUGGED_LOCAL_CANDIDATE_ROUTES,
    RUGGED_LOCAL_EXPERIMENTAL_ROUTES,
    SERVED_ROUTES,
    Route,
    RouteAlias,
)

# Keys may admit more routes than a picker offers. In particular, Ollama keys admit
# both wires, but the picker avoids the native adapter's Codex reasoning-option issue.
EMBEDDING_ROUTES: tuple[Route | RouteAlias, ...] = (
    GEMINI_EMBEDDING_ALIAS,
    *GEMINI_EMBEDDING_ROUTES,
    OLLAMA_EMBEDDING_ROUTE,
)
CHEAP_EXPERIMENTS_ROUTES = (
    *GEMINI_ROUTES,
    *ANTIGRAVITY_FLASH_LITE_ROUTES,
    *GEMINI_EMBEDDING_ROUTES,
    *MISTRAL_ROUTES,
    *OLLAMA_CHAT_ROUTES,
    *RUGGED_LOCAL_CANDIDATE_ROUTES,
    *RUGGED_LOCAL_EXPERIMENTAL_ROUTES,
    HAIKU_API,
    GPT6_LUNA_MESSAGES,
    GPT6_LUNA_RESPONSES,
)


@dataclass(frozen=True)
class ModelLaneRoutes:
    """Authorization and routing policy for a named group of LiteLLM clients.

    Allowed routes scope virtual keys. Fallbacks are ordered targets of a team's
    wildcard router rule; they must already be allowed and do not grant access.
    An empty fallback tuple means this lane has no configured fallback.
    """

    allowed: tuple[Route | RouteAlias, ...]
    fallbacks: tuple[Route, ...] = ()

    def __post_init__(self) -> None:
        if unserved := [route.id for route in self.allowed if route not in SERVED_ROUTES]:
            raise ValueError(f"lane selects unserved routes: {unserved}")
        if any(route not in self.allowed for route in self.fallbacks):
            raise ValueError("lane selects fallback routes outside its allowlist")


# Lane identifiers bind these policies to Terraform keys/teams; a key may combine lanes.
KEY_MODEL_LANES = {
    "gpt6_oai_lane_models": ModelLaneRoutes(allowed=GPT6_RESPONSES_ROUTES),
    "gpt6_codex_client_models": ModelLaneRoutes(allowed=GPT6_MESSAGES_ROUTES),
    "codex_client_models": ModelLaneRoutes(allowed=CHATGPT_MESSAGES_ROUTES, fallbacks=(GPT6_LUNA_MESSAGES,)),
    "claude_client_models": ModelLaneRoutes(allowed=ANTHROPIC_SUBSCRIPTION_ROUTES),
    "embedding_client_models": ModelLaneRoutes(allowed=EMBEDDING_ROUTES),
    "gemini_client_models": ModelLaneRoutes(allowed=GEMINI_ROUTES, fallbacks=(GEMINI_FLASH_LITE,)),
    "antigravity_client_models": ModelLaneRoutes(allowed=ANTIGRAVITY_ROUTES, fallbacks=(ANTIGRAVITY_FLASH_LITE,)),
    "ollama_chat_client_models": ModelLaneRoutes(allowed=OLLAMA_CHAT_ROUTES),
    "cheap_experiments_models": ModelLaneRoutes(allowed=CHEAP_EXPERIMENTS_ROUTES),
    "qwen_client_models": ModelLaneRoutes(
        allowed=(
            OLLAMA_QWEN_IQ4XS_128K.openai,
            OLLAMA_QWEN_IQ4XS_128K.native,
            OLLAMA_QWEN_IQ4XS_256K.openai,
            OLLAMA_QWEN_IQ4XS_256K.native,
        )
    ),
}
