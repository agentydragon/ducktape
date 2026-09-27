"""Agentplane-scoped display names for the models it offers, keyed by the raw upstream
model id -- the trailing segment of an exposed `{provider}/{shape}/{model}` LiteLLM
route name (see model_rosters.py).

Deliberately separate from model_rosters.py rather than an extension of it: that module
mixes typed, display-name-carrying rosters (CodexModel/AntigravityModel/GeminiModel)
with untyped `list[str]` ones (ANTHROPIC_MODELS, CLIPROXY_MODELS, OLLAMA_CHAT_MODELS),
and making it consistently structured is separate, larger work. This module reuses what
model_rosters.py already curates and fills in the rest with names scoped to what
agentplane actually offers today.
"""

from __future__ import annotations

from cluster.cdk8s.model_rosters import (
    ANTIGRAVITY_MODELS,
    OLLAMA_CHAT_MODELS,
    OPENCLAW_CODEX_MODELS,
    ollama_chat_variant,
)

_ANTHROPIC_DISPLAY_NAMES: dict[str, str] = {
    "claude-opus-5": "Opus 5",
    "claude-sonnet-5": "Sonnet 5",
    "claude-fable-5": "Fable 5",
    "claude-haiku-4-5-20251001": "Haiku 4.5",
}

# CLIPROXY_MODELS exposes these two alongside OPENCLAW_CODEX_MODELS's six, but that
# roster deliberately excludes them (unprobed serving-path limits) -- named here so
# agentplane can still offer them.
_UNPROBED_CODEX_DISPLAY_NAMES: dict[str, str] = {"gpt-5.4": "GPT-5.4", "gpt-5.5": "GPT-5.5"}

# OLLAMA_CHAT_MODELS's base id -> display name; the context-window suffix each exposed
# variant carries is computed below, mirroring `ollama_chat_variant`'s own suffixing.
_OLLAMA_BASE_DISPLAY_NAMES: dict[str, str] = {
    "qwen3.8-flash-next-iq4xs": "Qwen3.8 Flash Next IQ4_XS",
    "gpt-oss-20b": "GPT-OSS 20B",
    "gpt-oss-120b": "GPT-OSS 120B",
    "gemma4-31b-it-q8_0": "Gemma 4 31B",
}


def _ollama_variant_display_name(base_display_name: str, context: int) -> str:
    """Mirrors `model_rosters.ollama_chat_variant`'s id suffix, for display."""
    return f"{base_display_name} (1M)" if context == 1024 * 1024 else f"{base_display_name} ({context // 1024}K)"


_DISPLAY_NAMES: dict[str, str] = {
    **_ANTHROPIC_DISPLAY_NAMES,
    **{model.id: model.display_name for model in OPENCLAW_CODEX_MODELS},
    **_UNPROBED_CODEX_DISPLAY_NAMES,
    **{model.id: model.display_name for model in ANTIGRAVITY_MODELS},
    **{
        ollama_chat_variant(model, context): _ollama_variant_display_name(_OLLAMA_BASE_DISPLAY_NAMES[model], context)
        for model, _ollama_model, contexts in OLLAMA_CHAT_MODELS
        for context in contexts
    },
}


def display_name(exposed_name: str) -> str:
    """The human display name for an exposed `{provider}/{shape}/{model}` route (or a bare
    model id), from its trailing model segment. Raises for a model outside agentplane's
    closed, curated set -- an offered model with no display name is a bug to fix here, not
    a case to degrade past."""
    return _DISPLAY_NAMES[exposed_name.rsplit("/", 1)[-1]]
