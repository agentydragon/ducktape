"""Explicit consumer policy over canonical routes; no model facts or naming logic."""

from dataclasses import dataclass

from model_catalog.catalog import (
    ANTIGRAVITY_ROUTES,
    GEMINI_ROUTES,
    GPT6_LUNA_RESPONSES,
    GPT6_RESPONSES_ROUTES,
    OLLAMA_OPENAI_ROUTES,
    OLLAMA_QWEN_IQ4XS_ROUTES,
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


# Claude offerings are temporarily paused (#9121). Keep runner support, credentials,
# and served routes for existing sessions; re-enable these selections after validation.
STAGING_APP_MODELS = HarnessRoutes(claude=(), codex=(*GPT6_RESPONSES_ROUTES, *OLLAMA_OPENAI_ROUTES))
TESTING_APP_MODELS = HarnessRoutes(claude=(), codex=(GPT6_LUNA_RESPONSES, *OLLAMA_OPENAI_ROUTES))

# OpenClaw reserves maxTokens within contextWindow; omit routes without known limits.
PUBLIC_CODER_MODELS = (
    *GPT6_RESPONSES_ROUTES,
    *GEMINI_ROUTES,
    *(
        route
        for route in ANTIGRAVITY_ROUTES
        if route.model.context_window is not None and route.model.max_output_tokens is not None
    ),
)

# An explicit harness override policy, not all routes with known context metadata.
RUNNER_CONTEXT_OVERRIDES = OLLAMA_QWEN_IQ4XS_ROUTES
