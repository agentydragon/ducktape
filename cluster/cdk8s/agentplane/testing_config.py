"""Generates agentplane-testing's `agentplane-app-config` ConfigMap's `config.yaml`
content -- agentplane/app/main.py's `Settings`, mounted by the Deployment. See
model_rosters.py for the model-name scheme.
"""

from __future__ import annotations

from agentplane.app.action_federation import ActionFederationSettings
from cluster.cdk8s.agentplane.app_settings import AppSettingsConfig, settings
from cluster.cdk8s.litellm.keys import CHEAP_EXPERIMENTS_CLAUDE_MODEL, CHEAP_EXPERIMENTS_CODEX_MODEL

_NAMESPACE = "agentplane-testing"

# What the session form offers per harness: the one native model per harness the
# cheap-experiments key admits (tf/gitops/litellm-keys).
_HARNESS_CLAUDE = [CHEAP_EXPERIMENTS_CLAUDE_MODEL]
_HARNESS_CODEX = [CHEAP_EXPERIMENTS_CODEX_MODEL]


def config(action_federation: ActionFederationSettings | None = None) -> AppSettingsConfig:
    return settings(
        namespace=_NAMESPACE,
        harness_claude=_HARNESS_CLAUDE,
        harness_codex=_HARNESS_CODEX,
        thread_preset_codex_model=_HARNESS_CODEX[0],
        action_federation=action_federation,
    )
