"""Generates agentplane-testing/app/config.yaml's content -- x/agentplane/app/main.py's
`Settings`, mounted by the Deployment. See cluster/k8s/litellm/app/model_rosters.py for
the model-name scheme.
"""

from __future__ import annotations

from cluster.k8s.litellm.app.model_rosters import ApiShape, Provider, exposed_name

_NAMESPACE = "agentplane-testing"

# What the session form offers per harness: routes the cheap-experiments key may call
# (tf/gitops/litellm-keys), one native model per harness so far.
_HARNESS_CLAUDE = [exposed_name(Provider.ANTHROPIC_API, ApiShape.ANT_MESSAGES, "claude-haiku-4-5-20251001")]
_HARNESS_CODEX = [exposed_name(Provider.CHATGPT, ApiShape.OAI_RESPONSES, "gpt-5.6-luna")]

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
                "model": _HARNESS_CODEX[0],
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
        # (cluster/k8s/agentplane-testing/egress).
        "default_policies": ["basic"],
        # The egress proxy's admin port (x/agentplane/egress `Settings.admin_port`), asked
        # for each sandbox's recent decisions; until the proxy Deployment lands the page
        # shows the rules alone.
        "egress_admin_url": f"http://agentplane-egress-admin.{_NAMESPACE}.svc:8081",
    }
