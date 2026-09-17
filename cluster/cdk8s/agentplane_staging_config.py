"""Generates agentplane-staging/app/config.yaml's content -- x/agentplane/app/main.py's
`Settings`, mounted by the Deployment. See model_rosters.py for the model-name scheme.
"""

from __future__ import annotations

from cluster.cdk8s.agentplane_settings import agentplane_settings
from cluster.cdk8s.model_rosters import ANTHROPIC_MODELS, CLIPROXY_MODELS, ApiShape, Provider, exposed_name

_NAMESPACE = "agentplane-staging"

# Native subscription routes authorized by the staging key in tf/gitops/litellm-keys
# (claude_client_models/oai_lane_models there) -- kept in sync with the same source,
# model_rosters.py, that the Terraform locals derive from.
_HARNESS_CLAUDE = [exposed_name(Provider.ANTHROPIC_MAX20, ApiShape.ANT_MESSAGES, m) for m in ANTHROPIC_MODELS]
_HARNESS_CODEX = [exposed_name(Provider.CHATGPT, ApiShape.OAI_RESPONSES, m) for m in CLIPROXY_MODELS]


def config() -> dict:
    return agentplane_settings(
        namespace=_NAMESPACE,
        harness_claude=_HARNESS_CLAUDE,
        harness_codex=_HARNESS_CODEX,
        thread_preset_codex_model=exposed_name(Provider.CHATGPT, ApiShape.OAI_RESPONSES, "gpt-5.6-luna"),
        # What the console's `public_coder_github_reads` grants public-coder-agent
        # (cluster/k8s/haku/console/config.yaml), as this namespace's sets: reads of
        # confirmed-public repositories, of ducktape and its forks, and of the
        # private Gaffer repository.
        action_policy_sets=[
            "public-github-reads",
            "public-ducktape-reads",
            "public-ducktape-fork-reads",
            "public-gaffer-private-reads",
        ],
    )
