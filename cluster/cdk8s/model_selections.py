"""Explicit consumer policy over canonical routes; no model facts or naming logic."""

from dataclasses import dataclass

from agentplane.llm_ingress.models import ModelConfig
from model_catalog.catalog import (
    ANTIGRAVITY_GEMINI_38_FLASH_HIGH,
    ANTIGRAVITY_GEMINI_FLASH,
    ANTIGRAVITY_GEMINI_FLASH_LITE,
    ANTIGRAVITY_OPUS,
    ANTIGRAVITY_SONNET,
    GPT6_LUNA_RESPONSES,
    GPT6_RESPONSES_ROUTES,
    OLLAMA_OPENAI_ROUTES,
    OLLAMA_QWEN_IQ4XS_128K,
    OLLAMA_QWEN_IQ4XS_256K,
    Route,
)


@dataclass(frozen=True)
class HarnessRoutes:
    """Generation-time selections retained until app and ingress settings are emitted."""

    claude: tuple[Route, ...]
    codex: tuple[Route, ...]

    @property
    def all(self) -> tuple[Route, ...]:
        return tuple(dict.fromkeys((*self.claude, *self.codex)))


# Agentplane's hosted Claude and Codex pickers share the supported Antigravity Claude
# and Gemini routes. Claude uses Anthropic Messages; Codex reaches the same upstream
# through LiteLLM's Responses-to-Messages bridge. `TESTING_APP_MODELS`, the k3s fixture's
# local-model catalog, offers Claude only the local smoke model.
_AGENTPLANE_ANTIGRAVITY_ROUTES = (
    ANTIGRAVITY_OPUS,
    ANTIGRAVITY_SONNET,
    ANTIGRAVITY_GEMINI_FLASH,
    ANTIGRAVITY_GEMINI_FLASH_LITE,
    ANTIGRAVITY_GEMINI_38_FLASH_HIGH,
)

# The Agentplane Codex picker offers Qwen at 256K only; the 128K native route remains
# available in `TESTING_APP_MODELS` for the Claude smoke flow.
_AGENTPLANE_OLLAMA_OPENAI_ROUTES = tuple(
    route for route in OLLAMA_OPENAI_ROUTES if route != OLLAMA_QWEN_IQ4XS_128K.openai
)

STAGING_APP_MODELS = HarnessRoutes(
    claude=_AGENTPLANE_ANTIGRAVITY_ROUTES,
    codex=(*GPT6_RESPONSES_ROUTES, *_AGENTPLANE_ANTIGRAVITY_ROUTES, *_AGENTPLANE_OLLAMA_OPENAI_ROUTES),
)
TESTING_APP_MODELS = HarnessRoutes(
    claude=(OLLAMA_QWEN_IQ4XS_128K.native,), codex=(GPT6_LUNA_RESPONSES, *_AGENTPLANE_OLLAMA_OPENAI_ROUTES)
)

# Client context budgets, not inferred from Ollama num_ctx or provider limits. The ingress serves
# Claude's as `ModelConfig`; `codex_models.py` turns Codex's into catalog entries.
RUNNER_CONTEXT_OVERRIDES = {
    OLLAMA_QWEN_IQ4XS_128K.openai: 128 * 1024,
    OLLAMA_QWEN_IQ4XS_128K.native: 128 * 1024,
    OLLAMA_QWEN_IQ4XS_256K.openai: 256 * 1024,
    OLLAMA_QWEN_IQ4XS_256K.native: 256 * 1024,
}


def model_configs(models: HarnessRoutes) -> list[ModelConfig]:
    return [
        ModelConfig(model=route.id, total_context_budget_tokens=RUNNER_CONTEXT_OVERRIDES[route])
        for route in sorted(models.claude, key=lambda route: route.id)
        if route in RUNNER_CONTEXT_OVERRIDES
    ]
