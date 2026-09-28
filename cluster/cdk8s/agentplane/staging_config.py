"""Generates agentplane-staging's `agentplane-app-config` ConfigMap's `config.yaml`
content -- agentplane/app/main.py's `Settings`, mounted by the Deployment. See
model_rosters.py for the model-name scheme.
"""

from __future__ import annotations

from agentplane.app.action_federation import ActionFederationSettings
from agentplane.app.main import AppSettingsConfig
from cluster.cdk8s.agentplane.app_settings import OLLAMA_MODELS, settings
from cluster.cdk8s.litellm.keys import ANTIGRAVITY_CLIENT_MODELS, CLAUDE_CLIENT_MODELS, GPT6_OAI_LANE_MODELS
from cluster.cdk8s.model_rosters import ApiShape, Provider, codex_responses_name, exposed_name

_NAMESPACE = "agentplane-staging"
# The ActionPolicySet objects actions_staging_policies creates for the public-coder
# preset, named here because the preset binds them: reads of confirmed-public
# repositories, of ducktape and its fork, and of the private Gaffer repository.
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


def config(action_federation: ActionFederationSettings | None = None) -> AppSettingsConfig:
    return settings(
        namespace=_NAMESPACE,
        # The staging key admits GPT-6 subscription routes, the full Antigravity lineup,
        # and local Ollama chat routes.
        harness_claude=[*CLAUDE_CLIENT_MODELS, *ANTIGRAVITY_CLIENT_MODELS, *OLLAMA_MODELS],
        harness_codex=[*GPT6_OAI_LANE_MODELS, *OLLAMA_MODELS],
        thread_preset_codex_model=codex_responses_name("gpt-6-luna"),
        action_federation=action_federation,
        action_policy_sets=list(PUBLIC_CODER_ACTION_POLICY_SETS),
        # The "haku" sandbox preset (app_settings.py) exists only here, not in
        # agentplane-testing. claude-sonnet-5 to match what Haku's own managed agents run
        # today (haku/runtime/managed_agent/self_hosted/haku.agent.yaml).
        haku_preset_model=exposed_name(Provider.ANTHROPIC_MAX20, ApiShape.ANT_MESSAGES, "claude-sonnet-5"),
    )
