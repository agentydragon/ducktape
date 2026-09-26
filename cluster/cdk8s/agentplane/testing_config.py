"""Generates agentplane-testing's `agentplane-app-config` ConfigMap's `config.yaml`
content -- agentplane/app/main.py's `Settings`, mounted by the Deployment. See
model_rosters.py for the model-name scheme.
"""

from __future__ import annotations

from cluster.cdk8s.agentplane.app_settings import settings
from cluster.cdk8s.litellm.keys import (
    ANTIGRAVITY_CHEAP_CLIENT_MODELS,
    CHEAP_EXPERIMENTS_CLAUDE_MODEL,
    CHEAP_EXPERIMENTS_CODEX_MODEL,
    OLLAMA_CHAT_CLIENT_MODELS,
)

_NAMESPACE = "agentplane-testing"

# The cheap-experiments key admits these native models, Antigravity's flash-lite tier,
# and the local Ollama chat routes.
_HARNESS_CLAUDE = [CHEAP_EXPERIMENTS_CLAUDE_MODEL, *ANTIGRAVITY_CHEAP_CLIENT_MODELS, *OLLAMA_CHAT_CLIENT_MODELS]
_HARNESS_CODEX = [CHEAP_EXPERIMENTS_CODEX_MODEL, *OLLAMA_CHAT_CLIENT_MODELS]


def config() -> dict:
    return settings(
        namespace=_NAMESPACE,
        harness_claude=_HARNESS_CLAUDE,
        harness_codex=_HARNESS_CODEX,
        thread_preset_codex_model=_HARNESS_CODEX[0],
    )
