"""Generates agentplane-testing's `agentplane-app-config` ConfigMap's `config.yaml`
content -- agentplane/app/main.py's `Settings`, mounted by the Deployment. See
model_rosters.py for the model-name scheme.
"""

from __future__ import annotations

from cluster.cdk8s.agentplane.app_settings import settings
from cluster.cdk8s.litellm.keys import (
    CHEAP_EXPERIMENTS_CLAUDE_MODEL,
    CHEAP_EXPERIMENTS_CODEX_MODEL,
    LLAMA_CPP_CHAT_CLIENT_MODELS,
    OLLAMA_CHAT_CLIENT_MODELS,
)

_NAMESPACE = "agentplane-testing"

# Testing-only catalog for native, Ollama, and experimental llama.cpp chat routes.
_HARNESS_CLAUDE = [CHEAP_EXPERIMENTS_CLAUDE_MODEL, *OLLAMA_CHAT_CLIENT_MODELS, *LLAMA_CPP_CHAT_CLIENT_MODELS]
_HARNESS_CODEX = [CHEAP_EXPERIMENTS_CODEX_MODEL, *OLLAMA_CHAT_CLIENT_MODELS, *LLAMA_CPP_CHAT_CLIENT_MODELS]


def config() -> dict:
    return settings(
        namespace=_NAMESPACE,
        harness_claude=_HARNESS_CLAUDE,
        harness_codex=_HARNESS_CODEX,
        thread_preset_codex_model=_HARNESS_CODEX[0],
    )
