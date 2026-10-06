"""Generates agentplane-staging's `agentplane-app-config` ConfigMap's `config.yaml`
content -- agentplane/app/main.py's `Settings`, mounted by the Deployment. See
model_catalog/catalog.py for the model-name scheme.
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
    ACTIVITYWATCH_READ_POLICY,
    AGENTPLANE_TESTING_POLICY,
    AIQUOTA_READ_POLICY,
    BASIC_POLICY,
    BUILDBUDDY_POLICY,
    COINBASE_POLICY,
    DUCKTAPE_PR_INSTRUCTIONS,
    FINANCE_AIQUOTA_HISTORY_POLICY,
    FORGEJO_FINANCE_AGENT_POLICY,
    FORGEJO_HAKU_POLICY,
    GITHUB_ACTIONS_LOGS_POLICY,
    GITHUB_AGENTYDRAGON_AGENT_POLICY,
    GITHUB_CLONE_POLICY,
    GITHUB_IDENTITY_READS_SET,
    GOOGLE_READONLY_POLICY,
    GROCY_SF_READONLY_POLICY,
    HAKU_MAILBOX_POLICY,
    HOME_ASSISTANT_READONLY_POLICY,
    PACKAGES_POLICY,
    PLAID_PGWEB_POLICY,
    SSH_READS_SET,
    settings,
)
from cluster.cdk8s.agentplane.sandbox_pod import TOOL_CONFIG_READER_ROLE_NAME
from cluster.cdk8s.model_selections import STAGING_APP_MODELS
from model_catalog.catalog import GPT6_LUNA_RESPONSES, OLLAMA_QWEN_IQ4XS_256K

_NAMESPACE = "agentplane-staging"
_THREAD_PRESET_FINANCE_AGENT_CODEX = "finance-agent-codex"
_THREAD_PRESET_HAKU_CODEX = "haku-codex"
_HAKU_THREAD_SETUP = Path(__file__).with_name("haku_thread_setup.sh").read_text(encoding="utf-8")
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
DUCKTAPE_PR_FAILED_JOBS_SET = "ducktape-pr-failed-jobs"
FINANCE_AGENT_GAFFER_BRANCH_CREATION_SET = "finance-agent-gaffer-branch-creation"
FINANCE_AGENT_GAFFER_PR_CREATION_SET = "finance-agent-gaffer-pr-creation"
PUBLIC_CODER_ACTION_POLICY_SETS = (
    PUBLIC_GITHUB_READS_SET,
    PUBLIC_DUCKTAPE_READS_SET,
    PUBLIC_DUCKTAPE_FORK_READS_SET,
    PUBLIC_GAFFER_PRIVATE_READS_SET,
)


def config(
    action_federation: ActionFederationSettings | None = None,
    sandbox_service_grpc_channel_options: dict[str, int | str] | None = None,
) -> AppSettingsConfig:
    cfg = settings(
        namespace=_NAMESPACE,
        # The staging key admits GPT-6 subscription routes, the full Antigravity lineup,
        # and local Ollama chat routes.
        models=STAGING_APP_MODELS,
        thread_preset_codex_model=GPT6_LUNA_RESPONSES,
        action_federation=action_federation,
        sandbox_service_grpc_channel_options=sandbox_service_grpc_channel_options,
        action_policy_sets=[*PUBLIC_CODER_ACTION_POLICY_SETS, GITHUB_IDENTITY_READS_SET, SSH_READS_SET],
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
    cfg.sandbox_presets["public-coder"].kubernetes_grants = list(agent_access_profiles.MANAGED_GRANTS["public-coder"])
    # The "haku" and "finance-agent" launch presets live only here, not in app_settings.py:
    # they name staging-only credentials (haku: forgejo-haku, haku-mailbox; finance-agent:
    # forgejo-finance-agent, plaid-pgweb) that agentplane-testing never provisions, so there is
    # no second caller to share a definition with and nothing for `settings()` to compose.
    # Defining them here is what makes staging the only environment that ships them -- adding
    # one to agentplane-testing would mean provisioning its credentials first, not passing a
    # flag. Preset order here is the launch form's order.
    #
    # Haku launches on the local Qwen3.8 route with the wider 256K window the runner is
    # configured for; the OpenAI-compatible wire is the one that carries reasoning effort.
    # Both Qwen context windows stay offered, so an operator can pick the 128K one here.
    # TODO(#9121): consider switching Haku back to a Claude default once Anthropic models and
    # the Claude adapter are wired up and validated again. Existing Haku Threads stay on Codex
    # either way: a Thread cannot move to a model with a different configured context window.
    cfg.thread_presets[_THREAD_PRESET_HAKU_CODEX] = ThreadPreset(
        title="Haku",
        harness=Harness.CODEX,
        model=OLLAMA_QWEN_IQ4XS_256K.openai.id,
        cwd="/state/workspaces/{session_id}/haku-state",
        reasoning_effort="medium",
        # Keep Haku's identity and run-procedure pointer aligned with its other runtimes.
        # Add the hosted Thread's repository layout and durability rule.
        instructions=(
            "You are Haku, the operator's tireless background executive "
            "assistant. Your current directory is your haku-state checkout: "
            "your durable home for memory, notes, and other agent state. The "
            "ducktape checkout is at ../ducktape. Make changes in the checkout "
            "of the repository that owns them; never copy another repository "
            "into haku-state. Commit and push every persistent change to the "
            "owning repository's upstream or authorized fork as you go. A new "
            "Thread starts from fresh checkouts. Git auth is already in place. "
            "haku-state also holds who "
            "you are: read AGENTS.md, SOUL.md and MEMORY.md at its root, then "
            "your run procedure at memory/procedures/run.md. Read those, then "
            "execute the run procedure end to end."
        ),
        # The Forgejo egress policy substitutes the inert password placeholder
        # in the setup script with Haku's credential.
        setup_script=_HAKU_THREAD_SETUP,
    )
    cfg.sandbox_presets["haku"] = SandboxPreset(
        title="Haku",
        template="agentplane-runner",
        # These policies are what a *launch* is granted, independent of which
        # caller/ServiceAccount stamps it.
        egress_policies=[
            BASIC_POLICY,
            FORGEJO_HAKU_POLICY,
            PACKAGES_POLICY,
            GOOGLE_READONLY_POLICY,
            GROCY_SF_READONLY_POLICY,
            HOME_ASSISTANT_READONLY_POLICY,
            ACTIVITYWATCH_READ_POLICY,
            AIQUOTA_READ_POLICY,
            COINBASE_POLICY,
            HAKU_MAILBOX_POLICY,
            PLAID_PGWEB_POLICY,
            GITHUB_CLONE_POLICY,
            GITHUB_AGENTYDRAGON_AGENT_POLICY,
            GITHUB_ACTIONS_LOGS_POLICY,
            AGENTPLANE_TESTING_POLICY,
        ],
        action_policy_sets=[GITHUB_IDENTITY_READS_SET, SSH_READS_SET, DUCKTAPE_PR_FAILED_JOBS_SET],
        thread_preset=_THREAD_PRESET_HAKU_CODEX,
        kubernetes_grants=list(agent_access_profiles.MANAGED_GRANTS["haku"]),
        # Each new Thread gets its own haku-state and ducktape checkout.
    )
    cfg.thread_presets[_THREAD_PRESET_FINANCE_AGENT_CODEX] = ThreadPreset(
        title="Finance agent / Codex",
        harness=Harness.CODEX,
        model=GPT6_LUNA_RESPONSES.id,
        cwd="/state/workspaces/{session_id}/finance-agent",
        reasoning_effort="medium",
        instructions=_FINANCE_AGENT_INSTRUCTIONS,
        # The egress policy substitutes the inert Forgejo password placeholder in this script.
        setup_script=_FINANCE_AGENT_THREAD_SETUP,
    )
    cfg.sandbox_presets["finance-agent"] = SandboxPreset(
        title="Finance agent",
        template="agentplane-runner",
        egress_policies=[
            BASIC_POLICY,
            PACKAGES_POLICY,
            AIQUOTA_READ_POLICY,
            FINANCE_AIQUOTA_HISTORY_POLICY,
            COINBASE_POLICY,
            FORGEJO_FINANCE_AGENT_POLICY,
            PLAID_PGWEB_POLICY,
            GITHUB_AGENTYDRAGON_AGENT_POLICY,
            GITHUB_CLONE_POLICY,
            GITHUB_ACTIONS_LOGS_POLICY,
            BUILDBUDDY_POLICY,
        ],
        # Same GitHub read sets as public-coder: finance-agent forks/pushes/PRs ducktape
        # through the same agentydragon-agent account. Gaffer branch and PR creation are the only
        # additional auto-approved writes for this preset; pushes and other changes remain gated.
        action_policy_sets=[
            *PUBLIC_CODER_ACTION_POLICY_SETS,
            GITHUB_IDENTITY_READS_SET,
            SSH_READS_SET,
            FINANCE_AGENT_GAFFER_BRANCH_CREATION_SET,
            FINANCE_AGENT_GAFFER_PR_CREATION_SET,
        ],
        thread_preset=_THREAD_PRESET_FINANCE_AGENT_CODEX,
        kubernetes_grants=list(agent_access_profiles.MANAGED_GRANTS["finance-agent"]),
    )
    for preset in ("public-coder", "finance-agent"):
        cfg.sandbox_presets[preset].action_policy_sets.append(DUCKTAPE_PR_FAILED_JOBS_SET)
        cfg.sandbox_presets[preset].egress_policies.append(AGENTPLANE_TESTING_POLICY)
    return cfg
