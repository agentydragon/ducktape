"""Explicit consumer policy over canonical routes; no model facts or naming logic."""

from dataclasses import dataclass

from agentplane.llm_ingress.models import ModelConfig
from model_catalog.catalog import (
    ANTHROPIC_API_LATEST_ROUTES,
    ANTIGRAVITY_GEMINI_38_FLASH_HIGH,
    ANTIGRAVITY_GEMINI_FLASH,
    ANTIGRAVITY_GEMINI_FLASH_LITE,
    ANTIGRAVITY_OPUS,
    ANTIGRAVITY_SONNET,
    GPT6_RESPONSES_ROUTES,
    GPT6_SOL_RESPONSES,
    OLLAMA_OPENAI_ROUTES,
    OLLAMA_QWEN_IQ4XS_128K,
    OLLAMA_QWEN_IQ4XS_256K,
    Route,
)


@dataclass(frozen=True)
class HarnessRoutes:
    """Generation-time selections retained until app and ingress settings are emitted."""

    claude: tuple[Route, ...]
    # Responses routes shared by the Codex and central sampler pickers.
    codex: tuple[Route, ...]
    # Routes offered by the service-side central sampler. This is independent of Codex so
    # API-only models never become Codex models through the subscription-compatible wrapper.
    central_sampler: tuple[Route, ...] = ()

    @property
    def all(self) -> tuple[Route, ...]:
        return tuple(dict.fromkeys((*self.claude, *self.codex, *self.central_sampler)))


# Agentplane's hosted Claude and Codex pickers share the supported Antigravity Claude
# and Gemini routes. Claude uses Anthropic Messages; the central sampler reaches the same upstream
# through LiteLLM's Responses-to-Messages bridge.
_AGENTPLANE_ANTIGRAVITY_ROUTES = (
    ANTIGRAVITY_OPUS,
    ANTIGRAVITY_SONNET,
    ANTIGRAVITY_GEMINI_FLASH,
    ANTIGRAVITY_GEMINI_FLASH_LITE,
    ANTIGRAVITY_GEMINI_38_FLASH_HIGH,
)

# The Agentplane Codex picker offers Qwen at 256K only.
_AGENTPLANE_OLLAMA_OPENAI_ROUTES = tuple(
    route for route in OLLAMA_OPENAI_ROUTES if route != OLLAMA_QWEN_IQ4XS_128K.openai
)

# GPT-6 Sol remains served for other clients, but Agentplane exposes GPT-6.1 Sol instead.
_AGENTPLANE_GPT6_RESPONSES_ROUTES = tuple(route for route in GPT6_RESPONSES_ROUTES if route != GPT6_SOL_RESPONSES)

STAGING_APP_MODELS = HarnessRoutes(
    claude=_AGENTPLANE_ANTIGRAVITY_ROUTES,
    codex=(*_AGENTPLANE_GPT6_RESPONSES_ROUTES, *_AGENTPLANE_ANTIGRAVITY_ROUTES, *_AGENTPLANE_OLLAMA_OPENAI_ROUTES),
    central_sampler=(
        *_AGENTPLANE_GPT6_RESPONSES_ROUTES,
        *_AGENTPLANE_ANTIGRAVITY_ROUTES,
        *_AGENTPLANE_OLLAMA_OPENAI_ROUTES,
        *ANTHROPIC_API_LATEST_ROUTES,
    ),
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
