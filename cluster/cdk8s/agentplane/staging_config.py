"""Generates agentplane-staging's `agentplane-app-config` ConfigMap's `config.yaml`
content -- agentplane/app/main.py's `Settings`, mounted by the Deployment. See
model_rosters.py for the model-name scheme.
"""

from __future__ import annotations

from pathlib import Path

from agentplane.app.action_federation import ActionFederationSettings
from agentplane.app.main import AppSettingsConfig
from agentplane.app.presets import SandboxPreset, ThreadPreset
from agentplane.runner.harness import Harness
from agentplane.sandbox_service.kubernetes_grants import RoleBindingGrant, RoleRef
from cluster.cdk8s import agent_access_profiles
from cluster.cdk8s.agentplane.app_settings import (
    AGENTPLANE_TESTING_POLICY,
    AIQUOTA_READ_POLICY,
    BASIC_POLICY,
    BUILDBUDDY_POLICY,
    COINBASE_POLICY,
    DUCKTAPE_PR_INSTRUCTIONS,
    FORGEJO_FINANCE_AGENT_POLICY,
    GITHUB_ACTIONS_LOGS_POLICY,
    GITHUB_AGENTYDRAGON_AGENT_POLICY,
    GITHUB_CLONE_POLICY,
    GITHUB_IDENTITY_READS_SET,
    OLLAMA_MODELS,
    PACKAGES_POLICY,
    PLAID_PGWEB_POLICY,
    SSH_READS_SET,
    settings,
)
from cluster.cdk8s.agentplane.sandbox_pod import TOOL_CONFIG_READER_ROLE_NAME
from cluster.cdk8s.litellm.keys import ANTIGRAVITY_CLIENT_MODELS, CLAUDE_CLIENT_MODELS, GPT6_OAI_LANE_MODELS
from cluster.cdk8s.model_rosters import ApiShape, Provider, codex_responses_name, exposed_name

_NAMESPACE = "agentplane-staging"
_THREAD_PRESET_FINANCE_AGENT_CODEX = "finance-agent-codex"
_FINANCE_AGENT_INSTRUCTIONS = "\n\n".join(
    [
        Path(__file__).with_name("finance_agent_instructions.md").read_text(encoding="utf-8").strip(),
        DUCKTAPE_PR_INSTRUCTIONS,
    ]
)
_FINANCE_AGENT_THREAD_SETUP = Path(__file__).with_name("finance_agent_thread_setup.sh").read_text(encoding="utf-8")
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
    cfg = settings(
        namespace=_NAMESPACE,
        # The staging key admits GPT-6 subscription routes, the full Antigravity lineup,
        # and local Ollama chat routes.
        harness_claude=[*CLAUDE_CLIENT_MODELS, *ANTIGRAVITY_CLIENT_MODELS, *OLLAMA_MODELS],
        harness_codex=[*GPT6_OAI_LANE_MODELS, *OLLAMA_MODELS],
        thread_preset_codex_model=codex_responses_name("gpt-6-luna"),
        action_federation=action_federation,
        action_policy_sets=[*PUBLIC_CODER_ACTION_POLICY_SETS, GITHUB_IDENTITY_READS_SET, SSH_READS_SET],
        # The "haku" sandbox preset (app_settings.py) exists only here, not in
        # agentplane-testing. `claude-sonnet-5` matches the model in the parked self-hosted
        # configuration at haku/runtime/x/managed_agent/self_hosted/haku.agent.yaml.
        haku_preset_model=exposed_name(Provider.ANTHROPIC_MAX20, ApiShape.ANT_MESSAGES, "claude-sonnet-5"),
        kubernetes_grants={
            "sandbox-tool-config": RoleBindingGrant(
                kind="RoleBinding",
                namespace=_NAMESPACE,
                role_ref=RoleRef(kind="Role", name=TOOL_CONFIG_READER_ROLE_NAME),
            ),
            **agent_access_profiles.catalog(),
        },
        # Retain cleanup authority when a catalog choice is disabled while its
        # existing Sandboxes still hold a binding in that scope.
        kubernetes_binding_cleanup_namespaces=agent_access_profiles.cleanup_namespaces(),
        kubernetes_cluster_binding_cleanup=True,
    )
    for preset in ("public-coder", "haku"):
        cfg.sandbox_presets[preset].kubernetes_grants = list(agent_access_profiles.MANAGED_GRANTS[preset])
    # The "finance-agent" thread/sandbox presets live only here, not in app_settings.py:
    # they name staging-only credentials (forgejo-finance-agent, plaid-pgweb) that
    # agentplane-testing never provisions, and unlike "haku" they have no other caller,
    # so there's no shared definition to gate behind a parameter. Same model as
    # public-coder.
    cfg.thread_presets[_THREAD_PRESET_FINANCE_AGENT_CODEX] = ThreadPreset(
        title="Finance agent / Codex",
        harness=Harness.CODEX,
        model=codex_responses_name("gpt-6-luna"),
        cwd="/state/workspaces/{session_id}/finance-agent",
        reasoning_effort="medium",
        instructions=_FINANCE_AGENT_INSTRUCTIONS,
        # The egress policy substitutes the inert Forgejo password placeholder in this script.
        setup_script=_FINANCE_AGENT_THREAD_SETUP,
    )
    cfg.sandbox_presets["finance-agent"] = SandboxPreset(
        title="Finance agent",
        template="agentplane-runner",
        policies=[
            BASIC_POLICY,
            PACKAGES_POLICY,
            AIQUOTA_READ_POLICY,
            COINBASE_POLICY,
            FORGEJO_FINANCE_AGENT_POLICY,
            PLAID_PGWEB_POLICY,
            GITHUB_AGENTYDRAGON_AGENT_POLICY,
            GITHUB_CLONE_POLICY,
            GITHUB_ACTIONS_LOGS_POLICY,
            BUILDBUDDY_POLICY,
        ],
        # Same GitHub read sets as public-coder: finance-agent forks/pushes/PRs ducktape
        # through the same agentydragon-agent account, for the same generic-tooling role.
        action_policy_sets=[*PUBLIC_CODER_ACTION_POLICY_SETS, GITHUB_IDENTITY_READS_SET, SSH_READS_SET],
        thread_preset=_THREAD_PRESET_FINANCE_AGENT_CODEX,
        kubernetes_grants=list(agent_access_profiles.MANAGED_GRANTS["finance-agent"]),
    )
    for preset in ("public-coder", "finance-agent", "haku"):
        cfg.sandbox_presets[preset].policies.append(AGENTPLANE_TESTING_POLICY)
    return cfg
