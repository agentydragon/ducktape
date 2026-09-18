"""Generates agentplane-testing/app/config.yaml's content -- x/agentplane/app/main.py's
`Settings`, mounted by the Deployment. See model_rosters.py for the model-name scheme.
"""

from __future__ import annotations

from cluster.cdk8s.agentplane_settings import agentplane_settings
from cluster.cdk8s.model_rosters import ApiShape, Provider, exposed_name

_NAMESPACE = "agentplane-testing"

# What the session form offers per harness: routes the cheap-experiments key may call
# (tf/gitops/litellm-keys), one native model per harness so far.
_HARNESS_CLAUDE = [exposed_name(Provider.ANTHROPIC_API, ApiShape.ANT_MESSAGES, "claude-haiku-4-5-20251001")]
_HARNESS_CODEX = [exposed_name(Provider.CHATGPT, ApiShape.OAI_RESPONSES, "gpt-5.6-luna")]


def config() -> dict:
    return agentplane_settings(
        namespace=_NAMESPACE,
        harness_claude=_HARNESS_CLAUDE,
        harness_codex=_HARNESS_CODEX,
        thread_preset_codex_model=_HARNESS_CODEX[0],
    )
