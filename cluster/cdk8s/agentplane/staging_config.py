"""Generates agentplane-staging's `agentplane-app-config` ConfigMap's `config.yaml`
content -- agentplane/app/main.py's `Settings`, mounted by the Deployment. See
model_rosters.py for the model-name scheme.
"""

from __future__ import annotations

from cluster.cdk8s.agentplane.app_settings import (
    BASIC_POLICY,
    GITHUB_ACTIONS_LOGS_POLICY,
    GITHUB_AGENTYDRAGON_AGENT_POLICY,
    GITHUB_CLONE_POLICY,
    KUBERNETES_POLICY,
    PACKAGES_POLICY,
    settings,
)
from cluster.cdk8s.litellm.keys import (
    ANTIGRAVITY_CLIENT_MODELS,
    CLAUDE_CLIENT_MODELS,
    OAI_LANE_MODELS,
    OLLAMA_CHAT_CLIENT_MODELS,
)
from cluster.cdk8s.model_rosters import ApiShape, Provider, codex_responses_name, exposed_name

_NAMESPACE = "agentplane-staging"
_THREAD_PRESET_HAKU_CLAUDE = "haku-claude"
# The EgressPolicy objects egress_staging_credentials creates, named here because this
# namespace's own presets bind them and other agentplane-staging-only modules import
# them from here -- these policies exist only in agentplane-staging, unlike
# app_settings.py's, which every namespace shares.
FORGEJO_HAKU_POLICY = "forgejo-haku"
GOOGLE_READONLY_POLICY = "google-readonly"
GROCY_SF_READONLY_POLICY = "grocy-sf-readonly"
HOME_ASSISTANT_READONLY_POLICY = "home-assistant-readonly"
ACTIVITYWATCH_READ_POLICY = "activitywatch-read"
AIQUOTA_READ_POLICY = "aiquota-read"
HAKU_MAILBOX_POLICY = "haku-mailbox"
COINBASE_POLICY = "coinbase"
BUILDBUDDY_POLICY = "buildbuddy"
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
# claude-sonnet-5 to match what Haku's own managed agents run today
# (haku/runtime/managed_agent/self_hosted/haku.agent.yaml).
_HAKU_PRESET_MODEL = exposed_name(Provider.ANTHROPIC_MAX20, ApiShape.ANT_MESSAGES, "claude-sonnet-5")


def config() -> dict:
    result = settings(
        namespace=_NAMESPACE,
        # The staging key admits the subscription lanes, the full Antigravity lineup,
        # and local Ollama chat routes.
        harness_claude=[*CLAUDE_CLIENT_MODELS, *ANTIGRAVITY_CLIENT_MODELS, *OLLAMA_CHAT_CLIENT_MODELS],
        harness_codex=[*OAI_LANE_MODELS, *OLLAMA_CHAT_CLIENT_MODELS],
        thread_preset_codex_model=codex_responses_name("gpt-6-luna"),
        action_policy_sets=list(PUBLIC_CODER_ACTION_POLICY_SETS),
        public_coder_extra_policies=(BUILDBUDDY_POLICY,),
    )
    # The "haku" preset exists only in agentplane-staging, not agentplane-testing, so it's
    # built and merged in here rather than in the shared settings() both namespaces call.
    result["thread_presets"][_THREAD_PRESET_HAKU_CLAUDE] = {
        "title": "Haku",
        "harness": "HARNESS_CLAUDE",
        "model": _HAKU_PRESET_MODEL,
        "cwd": "/state/workspaces/{session_id}",
        "reasoning_effort": "medium",
        # Mirrors haku.agent.yaml's `system` prose (the same pointer the cloud and
        # self-hosted managed agents both carry): who reads what, and where the real
        # instructions live -- deliberately not duplicated here, so this preset cannot
        # drift from Haku's own run procedure.
        "instructions": (
            "You are Haku, the operator's tireless background executive assistant. "
            "Your haku-state checkout -- your memory, your method, and your only write "
            "surface -- is at haku-state, with git auth already in place. It also holds "
            "who you are: read AGENTS.md, SOUL.md and MEMORY.md at its root, then your "
            "run procedure at memory/procedures/run.md. Read those, then execute the run "
            "procedure end to end. Commit and push haku-state as you go."
        ),
    }
    result["sandbox_presets"]["haku"] = {
        "title": "Haku",
        "template": "agentplane-runner",
        # At least claude-ai's own reach (actions_staging_policies.py's EgressBinding for
        # haku-agent binds the same superset at the ServiceAccount level); listed again
        # here because a preset's `policies` are what a *launch* is granted, independent
        # of which caller/ServiceAccount stamps it.
        "policies": [
            BASIC_POLICY,
            KUBERNETES_POLICY,
            FORGEJO_HAKU_POLICY,
            PACKAGES_POLICY,
            GOOGLE_READONLY_POLICY,
            GROCY_SF_READONLY_POLICY,
            HOME_ASSISTANT_READONLY_POLICY,
            ACTIVITYWATCH_READ_POLICY,
            AIQUOTA_READ_POLICY,
            HAKU_MAILBOX_POLICY,
            COINBASE_POLICY,
            GITHUB_CLONE_POLICY,
            GITHUB_AGENTYDRAGON_AGENT_POLICY,
            GITHUB_ACTIONS_LOGS_POLICY,
        ],
        "thread_preset": _THREAD_PRESET_HAKU_CLAUDE,
        # Shallow clone of haku-state over the in-cluster Forgejo, the way
        # haku-sandbox-setup.sh clones it for Haku's own sandboxes
        # (cluster/k8s/haku/workspaces/image/haku-sandbox-setup.sh): --depth 1 because the
        # box only needs the HEAD checkout, not full history. The URL's userinfo carries
        # the literal placeholder string as the password half; git turns that into a
        # Basic Authorization header, and the `forgejo-haku` EgressPolicy's credentialRef
        # substitutes it for the `haku` Forgejo account's real password on the way out
        # (egress_staging_credentials.py) -- the placeholder itself is inert, so it is
        # safe to embed literally here. ducktape is cloned too, read-only reference the
        # same way Haku's own sandboxes carry it.
        "bootstrap": (
            "marker=/state/workspaces/.agentplane-haku-ready\n"
            "mkdir -p /state/workspaces\n"
            'if [ ! -f "$marker" ]; then\n'
            "  git clone --depth 1 --branch main --single-branch "
            "http://haku:agentplane-credential-forgejo-haku@"
            "forgejo-http.forgejo.svc.cluster.local:3000/haku/haku-state.git "
            "/state/workspaces/haku-state\n"
            "  git clone --depth 1 --branch devel --single-branch "
            "https://github.com/agentydragon/ducktape.git /state/workspaces/ducktape\n"
            "  printf '%s\\n' 'haku workspace initialized' > \"$marker\"\n"
            "fi\n"
        ),
    }
    return result
