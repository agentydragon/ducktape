"""Generates agentplane-staging's `agentplane-app-config` ConfigMap's `config.yaml`
content -- x/agentplane/app/main.py's `Settings`, mounted by the Deployment. See
model_rosters.py for the model-name scheme.
"""

from __future__ import annotations

from cluster.cdk8s.agentplane.app_settings import settings
from cluster.cdk8s.model_rosters import ANTHROPIC_MODELS, CLIPROXY_MODELS, ApiShape, Provider, exposed_name

_NAMESPACE = "agentplane-staging"
# The ActionPolicySet objects actions_staging_policies creates for the public-coder
# preset, named here because the preset binds them: reads of confirmed-public
# repositories, of ducktape and its fork, and of the private Gaffer repository -- what
# the console's `public_coder_github_reads` grants public-coder-agent
# (cluster/k8s/haku/console/config.yaml).
PUBLIC_GITHUB_READS_SET = "public-github-reads"
PUBLIC_DUCKTAPE_READS_SET = "public-ducktape-reads"
PUBLIC_DUCKTAPE_FORK_READS_SET = "public-ducktape-fork-reads"
PUBLIC_GAFFER_PRIVATE_READS_SET = "public-gaffer-private-reads"
PUBLIC_CODER_ACTION_POLICY_SETS = (
    PUBLIC_GITHUB_READS_SET,
    PUBLIC_DUCKTAPE_READS_SET,
    PUBLIC_DUCKTAPE_FORK_READS_SET,
    PUBLIC_GAFFER_PRIVATE_READS_SET,
)

# Native subscription routes authorized by the staging key in tf/gitops/litellm-keys
# (claude_client_models/oai_lane_models there) -- kept in sync with the same source,
# model_rosters.py, that the Terraform locals derive from.
_HARNESS_CLAUDE = [exposed_name(Provider.ANTHROPIC_MAX20, ApiShape.ANT_MESSAGES, m) for m in ANTHROPIC_MODELS]
_HARNESS_CODEX = [exposed_name(Provider.CHATGPT, ApiShape.OAI_RESPONSES, m) for m in CLIPROXY_MODELS]


def config() -> dict:
    return settings(
        namespace=_NAMESPACE,
        harness_claude=_HARNESS_CLAUDE,
        harness_codex=_HARNESS_CODEX,
        thread_preset_codex_model=exposed_name(Provider.CHATGPT, ApiShape.OAI_RESPONSES, "gpt-5.6-luna"),
        action_policy_sets=list(PUBLIC_CODER_ACTION_POLICY_SETS),
    )
