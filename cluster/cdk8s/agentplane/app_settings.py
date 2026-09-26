"""Shared agentplane/app/main.py `Settings` shape assembled by both
staging_config.py and testing_config.py, which own the per-namespace model
routes and policies passed in here.
"""

from __future__ import annotations

_THREAD_PRESET_PUBLIC_CODER_CODEX = "public-coder-codex"
# The EgressPolicy objects egress creates in every environment, named here
# because the presets bind them.
BASIC_POLICY = "basic"
GITHUB_PUBLIC_POLICY = "github-public"
FORGEJO_HAKU_POLICY = "forgejo-haku"
PACKAGES_POLICY = "packages"
KUBERNETES_POLICY = "kubernetes"
GOOGLE_READONLY_POLICY = "google-readonly"
GROCY_SF_READONLY_POLICY = "grocy-sf-readonly"
HOME_ASSISTANT_READONLY_POLICY = "home-assistant-readonly"
ACTIVITYWATCH_READ_POLICY = "activitywatch-read"
AIQUOTA_READ_POLICY = "aiquota-read"
HAKU_MAILBOX_POLICY = "haku-mailbox"
COINBASE_POLICY = "coinbase"


def settings(
    *,
    namespace: str,
    harness_claude: list[str],
    harness_codex: list[str],
    thread_preset_codex_model: str,
    action_policy_sets: list[str] | None = None,
) -> dict:
    return {
        "models": {"HARNESS_CLAUDE": harness_claude, "HARNESS_CODEX": harness_codex},
        # Rendered into the image-owned agent-instruction template; deployments may use
        # different service names.
        "agent_egress_api_url": f"http://agentplane-egress.{namespace}.svc.cluster.local",
        "agent_actions_service_url": f"http://agentplane-actions.{namespace}.svc.cluster.local:8080",
        # App-owned launch-form presets. The browser expands one into editable concrete
        # template, policy, bootstrap, and SessionSpec fields; neither a Sandbox CR nor a
        # runner receives a preset name.
        "thread_presets": {
            _THREAD_PRESET_PUBLIC_CODER_CODEX: {
                "title": "Public coder / Codex",
                "harness": "HARNESS_CODEX",
                "model": thread_preset_codex_model,
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
                "policies": [BASIC_POLICY, GITHUB_PUBLIC_POLICY],
                **({"action_policy_sets": action_policy_sets} if action_policy_sets is not None else {}),
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
        # endpoint a sandbox has no agent, so it is not a choice (see this namespace's
        # egress/ directory).
        "default_policies": [BASIC_POLICY],
        # The egress proxy's admin port (agentplane/egress `Settings.admin_port`), asked
        # for each sandbox's recent decisions; until the proxy Deployment lands the page
        # shows the rules alone.
        "egress_admin_url": f"http://agentplane-egress-admin.{namespace}.svc:8081",
    }
