"""Generates agentplane-staging's `agentplane-app-config` ConfigMap's `config.yaml`
content -- x/agentplane/app/main.py's `Settings`, mounted by the Deployment. See
model_rosters.py for the model-name scheme.
"""

from __future__ import annotations

from cluster.cdk8s.agentplane.app_settings import settings
from cluster.cdk8s.litellm.keys import CLAUDE_CLIENT_MODELS, OAI_LANE_MODELS
from cluster.cdk8s.model_rosters import codex_responses_name

_NAMESPACE = "agentplane-staging"
# The ActionPolicySet objects actions_staging_policies creates for the public-coder
# preset, named here because the preset binds them: reads of confirmed-public
# repositories, of ducktape and its fork, and of the private Gaffer repository -- what
# the console's `public_coder_github_reads` grants public-coder-agent
# (cluster/cdk8s/haku/console_config.py).
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


def config() -> dict:
    return settings(
        namespace=_NAMESPACE,
        # What the session form offers per harness: the native subscription lanes the
        # staging key admits (litellm_key.agentplane_staging in tf/gitops/litellm-keys).
        harness_claude=CLAUDE_CLIENT_MODELS,
        harness_codex=OAI_LANE_MODELS,
        thread_preset_codex_model=codex_responses_name("gpt-5.6-luna"),
        action_policy_sets=list(PUBLIC_CODER_ACTION_POLICY_SETS),
    )
