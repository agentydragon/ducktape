"""Explicit consumer policy over canonical routes; no model facts or naming logic."""

from dataclasses import dataclass

from agentplane.llm_ingress.models import ModelConfig
from model_catalog.catalog import (
    ANTIGRAVITY_OPUS,
    ANTIGRAVITY_PRO,
    ANTIGRAVITY_PRO_LOW,
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


# Agentplane's hosted Claude and Codex pickers share these four Antigravity routes. The
# Claude API adapter is Anthropic Messages; Codex reaches the same upstream through
# LiteLLM's Responses-to-Messages bridge. Testing offers Claude only its local smoke model.
_AGENTPLANE_ANTIGRAVITY_ROUTES = (ANTIGRAVITY_OPUS, ANTIGRAVITY_SONNET, ANTIGRAVITY_PRO, ANTIGRAVITY_PRO_LOW)

# The Agentplane Codex picker offers Qwen at 256K only; the 128K native route remains
# available in testing for the live Claude smoke flow.
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

# Preserve the existing runner-owned context overrides for both native harnesses.
# These are client budgets, not inferred from Ollama num_ctx or provider limits.
RUNNER_CONTEXT_OVERRIDES = {
    OLLAMA_QWEN_IQ4XS_128K.openai: 128 * 1024,
    OLLAMA_QWEN_IQ4XS_128K.native: 128 * 1024,
    OLLAMA_QWEN_IQ4XS_256K.openai: 256 * 1024,
    OLLAMA_QWEN_IQ4XS_256K.native: 256 * 1024,
}


def model_configs(models: HarnessRoutes) -> list[ModelConfig]:
    return [
        ModelConfig(model=route.id, total_context_budget_tokens=RUNNER_CONTEXT_OVERRIDES[route])
        for route in sorted(models.all, key=lambda route: route.id)
        if route in RUNNER_CONTEXT_OVERRIDES
    ]
