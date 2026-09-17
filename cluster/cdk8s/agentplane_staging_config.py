"""Generates agentplane-staging/app/config.yaml's content -- x/agentplane/app/main.py's
`Settings`, mounted by the Deployment. See model_rosters.py for the model-name scheme.
"""

from __future__ import annotations

from cluster.cdk8s.model_rosters import ANTHROPIC_MODELS, CLIPROXY_MODELS, ApiShape, Provider, exposed_name

_NAMESPACE = "agentplane-staging"

# Native subscription routes authorized by the staging key in tf/gitops/litellm-keys
# (claude_client_models/oai_lane_models there) -- kept in sync with the same source,
# model_rosters.py, that the Terraform locals derive from.
_HARNESS_CLAUDE = [exposed_name(Provider.ANTHROPIC_MAX20, ApiShape.ANT_MESSAGES, m) for m in ANTHROPIC_MODELS]
_HARNESS_CODEX = [exposed_name(Provider.CHATGPT, ApiShape.OAI_RESPONSES, m) for m in CLIPROXY_MODELS]

_CODEX_DEFAULT_MODEL = exposed_name(Provider.CHATGPT, ApiShape.OAI_RESPONSES, "gpt-5.6-luna")

_THREAD_PRESET_PUBLIC_CODER_CODEX = "public-coder-codex"


def config() -> dict:
    return {
        "models": {"HARNESS_CLAUDE": _HARNESS_CLAUDE, "HARNESS_CODEX": _HARNESS_CODEX},
        # Rendered into the image-owned agent-instruction template; deployments may use
        # different service names.
        "agent_egress_api_url": f"http://agentplane-egress.{_NAMESPACE}.svc.cluster.local",
        "agent_actions_service_url": f"http://agentplane-actions.{_NAMESPACE}.svc.cluster.local:8080",
        # App-owned launch-form presets. The browser expands one into editable concrete
        # template, policy, bootstrap, and SessionSpec fields; neither a Sandbox CR nor a
        # runner receives a preset name.
        "thread_presets": {
            _THREAD_PRESET_PUBLIC_CODER_CODEX: {
                "title": "Public coder / Codex",
                "harness": "HARNESS_CODEX",
                "model": _CODEX_DEFAULT_MODEL,
                "cwd": "/state/workspaces/{session_id}",
                "reasoning_effort": "medium",
                "instructions": (
                    "Work as a public-repository coding agent. Keep private cluster data "
                    "out of the workspace and outputs."
                ),
            }
        },
        "sandbox_presets": {
            "public-coder": {
                "title": "Public coder",
                "template": "agentplane-runner",
                "policies": ["basic", "github-public"],
                # What the console's `public_coder_github_reads` grants public-coder-agent
                # (cluster/k8s/haku/console/config.yaml), as this namespace's sets: reads of
                # confirmed-public repositories, of ducktape and its forks, and of the
                # private Gaffer repository.
                "action_policy_sets": [
                    "public-github-reads",
                    "public-ducktape-reads",
                    "public-ducktape-fork-reads",
                    "public-gaffer-private-reads",
                ],
                "thread_preset": _THREAD_PRESET_PUBLIC_CODER_CODEX,
                "bootstrap": (
                    "marker=/state/workspaces/.agentplane-public-coder-ready\n"
                    "mkdir -p /state/workspaces\n"
                    'if [ ! -f "$marker" ]; then\n'
                    "  printf '%s\\n' 'public-coder workspace initialized' > \"$marker\"\n"
                    "fi\n"
                ),
            }
        },
        # Granted to every sandbox before whatever the operator picks: without the model
        # endpoint a sandbox has no agent, so it is not a choice
        # (cluster/k8s/agentplane-staging/egress).
        "default_policies": ["basic"],
        # The egress proxy's admin port (x/agentplane/egress `Settings.admin_port`), asked
        # for each sandbox's recent decisions; until the proxy Deployment lands the page
        # shows the rules alone.
        "egress_admin_url": f"http://agentplane-egress-admin.{_NAMESPACE}.svc:8081",
    }
