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
    TABBYAPI_QWEN_ABLITERATED,
    TABBYAPI_QWEN_STOCK,
    Route,
)


@dataclass(frozen=True)
class HarnessRoutes:
    """Generation-time selections retained until app and ingress settings are emitted."""

    claude: tuple[Route, ...]
    # Routes offered by Codex through the Responses API.
    codex: tuple[Route, ...]
    # Routes offered by the service-side central sampler. This is independent of Codex so
    # API-only models never become Codex models through the subscription-compatible wrapper.
    central_sampler: tuple[Route, ...] = ()
    central_sampler_chat: tuple[Route, ...] = ()

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

# GPT-6 Sol remains served for other clients, but Agentplane exposes GPT-6.1 Sol instead.
_AGENTPLANE_GPT6_RESPONSES_ROUTES = tuple(route for route in GPT6_RESPONSES_ROUTES if route != GPT6_SOL_RESPONSES)


def chat_completions_id(route: Route, responses_routes: tuple[Route, ...]) -> str:
    return f"{route.id}-chat-completions" if route in responses_routes else route.id


STAGING_APP_MODELS = HarnessRoutes(
    central_sampler_chat=(TABBYAPI_QWEN_STOCK, TABBYAPI_QWEN_ABLITERATED),
    claude=_AGENTPLANE_ANTIGRAVITY_ROUTES,
    codex=(
        *_AGENTPLANE_GPT6_RESPONSES_ROUTES,
        *_AGENTPLANE_ANTIGRAVITY_ROUTES,
        TABBYAPI_QWEN_STOCK,
        TABBYAPI_QWEN_ABLITERATED,
    ),
    central_sampler=(
        *_AGENTPLANE_GPT6_RESPONSES_ROUTES,
        *_AGENTPLANE_ANTIGRAVITY_ROUTES,
        TABBYAPI_QWEN_STOCK,
        *ANTHROPIC_API_LATEST_ROUTES,
    ),
)

# Client context budgets, not inferred from Ollama num_ctx or provider limits. The ingress serves
# Claude's as `ModelConfig`; `codex_models.py` turns Codex's into catalog entries.
RUNNER_CONTEXT_OVERRIDES = {TABBYAPI_QWEN_ABLITERATED: 256 * 1024, TABBYAPI_QWEN_STOCK: 256 * 1024}


def model_configs(models: HarnessRoutes) -> list[ModelConfig]:
    return [
        ModelConfig(model=route.id, total_context_budget_tokens=RUNNER_CONTEXT_OVERRIDES[route])
        for route in sorted(models.claude, key=lambda route: route.id)
        if route in RUNNER_CONTEXT_OVERRIDES
    ]
