"""Explicit consumer policy over canonical routes; no model facts or naming logic."""

from dataclasses import dataclass

from model_catalog.catalog import (
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


# Claude offerings are temporarily paused (#9121). Keep runner support, credentials,
# and served routes for existing sessions; re-enable these selections after validation.
STAGING_APP_MODELS = HarnessRoutes(claude=(), codex=(*GPT6_RESPONSES_ROUTES, *OLLAMA_OPENAI_ROUTES))
TESTING_APP_MODELS = HarnessRoutes(claude=(), codex=(GPT6_LUNA_RESPONSES, *OLLAMA_OPENAI_ROUTES))

# Preserve the existing runner-owned context overrides for both native harnesses.
# These are client budgets, not inferred from Ollama num_ctx or provider limits.
RUNNER_CONTEXT_OVERRIDES = {
    OLLAMA_QWEN_IQ4XS_128K.openai: 128 * 1024,
    OLLAMA_QWEN_IQ4XS_128K.native: 128 * 1024,
    OLLAMA_QWEN_IQ4XS_256K.openai: 256 * 1024,
    OLLAMA_QWEN_IQ4XS_256K.native: 256 * 1024,
}
