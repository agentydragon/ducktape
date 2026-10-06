"""Shared agentplane/app/main.py `Settings` shape assembled by both
staging_config.py and testing_config.py, which own the per-namespace model
routes and policies passed in here.
"""

from __future__ import annotations

from pathlib import Path

from agentplane.app.action_federation import ActionFederationSettings
from agentplane.app.api import ModelCatalog, ModelOption
from agentplane.app.main import AppSettingsConfig
from agentplane.app.presets import SandboxPreset, ThreadPreset
from agentplane.runner.harness import Harness
from agentplane.sandbox_service.kubernetes_grants import KubernetesGrant
from cluster.cdk8s.model_selections import HarnessRoutes
from model_catalog.catalog import Route

_THREAD_PRESET_PUBLIC_CODER_CODEX = "public-coder-codex"
_THREAD_PRESET_HAKU_CLAUDE = "haku-claude"
# The EgressPolicy objects egress creates in every environment, named here
# because presets and explicit grants refer to them.
BASIC_POLICY = "basic"
INFERENCE_EXPERIMENTS_POLICY = "inference-experiments"
GITHUB_AGENTYDRAGON_AGENT_POLICY = "github-agentydragon-agent"
GITHUB_CLONE_POLICY = "github-clone"
GITHUB_ACTIONS_LOGS_POLICY = "github-actions-logs"
FORGEJO_HAKU_POLICY = "forgejo-haku"
FORGEJO_FINANCE_AGENT_POLICY = "forgejo-finance-agent"
AGENTPLANE_TESTING_POLICY = "agentplane-testing"
PACKAGES_POLICY = "packages"
PUBLIC_INTERNET_POLICY = "public-internet"
GOOGLE_READONLY_POLICY = "google-readonly"
GROCY_SF_READONLY_POLICY = "grocy-sf-readonly"
HOME_ASSISTANT_READONLY_POLICY = "home-assistant-readonly"
ACTIVITYWATCH_READ_POLICY = "activitywatch-read"
AIQUOTA_READ_POLICY = "aiquota-read"
FINANCE_AIQUOTA_HISTORY_POLICY = "finance-aiquota-history"
HAKU_MAILBOX_POLICY = "haku-mailbox"
COINBASE_POLICY = "coinbase"
BUILDBUDDY_POLICY = "buildbuddy"
PLAID_PGWEB_POLICY = "plaid-pgweb"
# The ActionPolicySet actions_staging_policies creates for the caller's own GitHub identity
# (`get_me`, no repository or mutation surface) -- every preset binds it by default, named
# here (not alongside the EgressPolicy names above) because it is a different CRD kind.
GITHUB_IDENTITY_READS_SET = "github-identity-reads"
SSH_READS_SET = "ssh-reads"


DUCKTAPE_PR_INSTRUCTIONS = Path(__file__).with_name("ducktape_pr_instructions.md").read_text(encoding="utf-8").strip()
_PUBLIC_CODER_INSTRUCTIONS = "\n\n".join(
    [
        Path(__file__).with_name("public_coder_instructions.md").read_text(encoding="utf-8").strip(),
        DUCKTAPE_PR_INSTRUCTIONS,
    ]
)
_HAKU_THREAD_SETUP = Path(__file__).with_name("haku_thread_setup.sh").read_text(encoding="utf-8")


def settings(
    *,
    namespace: str,
    models: HarnessRoutes,
    thread_preset_codex_model: Route,
    action_federation: ActionFederationSettings | None = None,
    action_policy_sets: list[str] | None = None,
    sandbox_service_grpc_channel_options: dict[str, int | str] | None = None,
    haku_preset_model: Route | None = None,
    kubernetes_grants: dict[str, KubernetesGrant] | None = None,
    kubernetes_binding_cleanup_namespaces: list[str] | None = None,
    kubernetes_cluster_binding_cleanup: bool = False,
) -> AppSettingsConfig:
    return AppSettingsConfig(
        sandbox_service_grpc_channel_options=sandbox_service_grpc_channel_options or {},
        models=ModelCatalog(
            models=[
                ModelOption(
                    model=route.id, display_name=route.display_name, reasoning_efforts=list(route.reasoning_efforts)
                )
                for route in models.all
            ],
            harnesses={
                Harness.CLAUDE: [route.id for route in models.claude],
                Harness.CODEX: [route.id for route in models.codex],
            },
        ),
        kubernetes_grants=kubernetes_grants if kubernetes_grants is not None else {},
        **(
            {"kubernetes_binding_cleanup_namespaces": kubernetes_binding_cleanup_namespaces}
            if kubernetes_binding_cleanup_namespaces
            else {}
        ),
        **({"kubernetes_cluster_binding_cleanup": True} if kubernetes_cluster_binding_cleanup else {}),
        # App-owned launch-form presets. The browser expands one into editable concrete
        # template, policy, bootstrap, and SessionSpec fields; neither a Sandbox CR nor a
        # runner receives a preset name.
        thread_presets={
            _THREAD_PRESET_PUBLIC_CODER_CODEX: ThreadPreset(
                title="Public coder / Codex",
                harness=Harness.CODEX,
                model=thread_preset_codex_model.id,
                cwd="/state/workspaces/{session_id}",
                reasoning_effort="medium",
                instructions=_PUBLIC_CODER_INSTRUCTIONS,
            ),
            **(
                {
                    _THREAD_PRESET_HAKU_CLAUDE: ThreadPreset(
                        title="Haku",
                        harness=Harness.CLAUDE,
                        model=haku_preset_model.id,
                        cwd="/state/workspaces/{session_id}/haku-state",
                        reasoning_effort="medium",
                        # Keep Haku's identity and run-procedure pointer aligned with its other
                        # runtimes. Add the hosted Thread's repository layout and durability rule.
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
                }
                if haku_preset_model is not None
                else {}
            ),
        },
        sandbox_presets={
            "public-coder": SandboxPreset(
                title="Public coder",
                template="agentplane-runner",
                policies=[
                    BASIC_POLICY,
                    PACKAGES_POLICY,
                    GITHUB_AGENTYDRAGON_AGENT_POLICY,
                    GITHUB_CLONE_POLICY,
                    GITHUB_ACTIONS_LOGS_POLICY,
                    BUILDBUDDY_POLICY,
                ],
                **({"action_policy_sets": action_policy_sets} if action_policy_sets is not None else {}),
                thread_preset=_THREAD_PRESET_PUBLIC_CODER_CODEX,
                bootstrap=(
                    "marker=/state/workspaces/.agentplane-public-coder-ready\n"
                    "mkdir -p /state/workspaces\n"
                    'if [ ! -f "$marker" ]; then\n'
                    "  printf '%s\\n' 'public-coder workspace initialized' > \"$marker\"\n"
                    "fi\n"
                ),
            ),
            **(
                {
                    "haku": SandboxPreset(
                        title="Haku",
                        template="agentplane-runner",
                        # These policies are what a *launch* is granted, independent of
                        # which caller/ServiceAccount stamps it.
                        policies=[
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
                        ],
                        action_policy_sets=[GITHUB_IDENTITY_READS_SET, SSH_READS_SET],
                        thread_preset=_THREAD_PRESET_HAKU_CLAUDE,
                        # Each new Thread gets its own haku-state and ducktape checkout.
                    )
                }
                if haku_preset_model is not None
                else {}
            ),
        },
        # Grant platform operations and inference experiments independently. The latter
        # is a fleet default, not part of the basic Agentplane platform policy.
        default_policies=[BASIC_POLICY, INFERENCE_EXPERIMENTS_POLICY],
        # The egress proxy's admin port (agentplane/egress `Settings.admin_port`), asked
        # for each sandbox's recent decisions; until the proxy Deployment lands the page
        # shows the rules alone.
        egress_admin_url=f"http://agentplane-egress-admin.{namespace}.svc:8081",
        action_federation=action_federation,
    )
