"""Shared agentplane/app/main.py `Settings` shape assembled by both
staging_config.py and testing_config.py, which own the per-namespace model
routes and policies passed in here.
"""

from __future__ import annotations

from pathlib import Path

from agentplane.app.action_federation import ActionFederationSettings
from agentplane.app.api import ModelCatalog, ModelOption
from agentplane.app.main import AppSettingsConfig
from agentplane.app.presets import Harness, SandboxPreset, ThreadPreset
from cluster.cdk8s.agentplane.model_display_names import display_name
from cluster.cdk8s.model_rosters import OLLAMA_CHAT_MODELS, ApiShape, Provider, exposed_name, ollama_chat_variant

# Offer one route per local model/context, avoiding the native adapter's
# incompatible handling of Codex's reasoning options.
OLLAMA_MODELS = [
    exposed_name(Provider.OLLAMA, ApiShape.OAI_CHAT, ollama_chat_variant(model, context))
    for model, _, contexts in OLLAMA_CHAT_MODELS
    for context in contexts
]

_THREAD_PRESET_PUBLIC_CODER_CODEX = "public-coder-codex"
_THREAD_PRESET_HAKU_CLAUDE = "haku-claude"
# The EgressPolicy objects egress creates in every environment, named here
# because the presets bind them.
BASIC_POLICY = "basic"
GITHUB_AGENTYDRAGON_AGENT_POLICY = "github-agentydragon-agent"
GITHUB_CLONE_POLICY = "github-clone"
GITHUB_ACTIONS_LOGS_POLICY = "github-actions-logs"
FORGEJO_HAKU_POLICY = "forgejo-haku"
FORGEJO_FINANCE_AGENT_POLICY = "forgejo-finance-agent"
PACKAGES_POLICY = "packages"
GOOGLE_READONLY_POLICY = "google-readonly"
GROCY_SF_READONLY_POLICY = "grocy-sf-readonly"
HOME_ASSISTANT_READONLY_POLICY = "home-assistant-readonly"
ACTIVITYWATCH_READ_POLICY = "activitywatch-read"
AIQUOTA_READ_POLICY = "aiquota-read"
HAKU_MAILBOX_POLICY = "haku-mailbox"
COINBASE_POLICY = "coinbase"
BUILDBUDDY_POLICY = "buildbuddy"
PLAID_PGWEB_POLICY = "plaid-pgweb"


_PUBLIC_CODER_INSTRUCTIONS = (
    Path(__file__).with_name("public_coder_instructions.md").read_text(encoding="utf-8").strip()
)
_HAKU_BOOTSTRAP = Path(__file__).with_name("haku_bootstrap.sh").read_text(encoding="utf-8")


def reasoning_efforts(model: str) -> list[str]:
    """Documented reasoning effort values for the configured direct Anthropic/OpenAI routes."""
    if model.startswith(("anthropic-max20/ant-messages/", "antigravity/ant-messages/claude-")):
        return ["low", "medium", "high", "max"]
    if model.startswith("chatgpt/oai-responses/gpt-"):
        return ["minimal", "low", "medium", "high", "xhigh"]
    # Other providers/routes in this roster do not expose these reasoning effort parameters.
    return []


def settings(
    *,
    namespace: str,
    harness_claude: list[str],
    harness_codex: list[str],
    thread_preset_codex_model: str,
    action_federation: ActionFederationSettings | None = None,
    action_policy_sets: list[str] | None = None,
    haku_preset_model: str | None = None,
) -> AppSettingsConfig:
    # A model both harnesses accept (e.g. a local Ollama route) names its display name once,
    # regardless of how many harness lists reference it. dict.fromkeys dedupes while keeping
    # each model's first-seen order.
    all_models = dict.fromkeys((*harness_claude, *harness_codex))
    return AppSettingsConfig(
        models=ModelCatalog(
            models=[
                ModelOption(model=model, display_name=display_name(model), reasoning_efforts=reasoning_efforts(model))
                for model in all_models
            ],
            harnesses={Harness.CLAUDE: harness_claude, Harness.CODEX: harness_codex},
        ),
        # Rendered into the image-owned agent-instruction template; deployments may use
        # different service names.
        agent_egress_api_url=f"http://agentplane-egress.{namespace}.svc.cluster.local",
        agent_actions_service_url=f"http://agentplane-actions.{namespace}.svc.cluster.local:8080",
        # App-owned launch-form presets. The browser expands one into editable concrete
        # template, policy, bootstrap, and SessionSpec fields; neither a Sandbox CR nor a
        # runner receives a preset name.
        thread_presets={
            _THREAD_PRESET_PUBLIC_CODER_CODEX: ThreadPreset(
                title="Public coder / Codex",
                harness=Harness.CODEX,
                model=thread_preset_codex_model,
                cwd="/state/workspaces/{session_id}",
                reasoning_effort="medium",
                instructions=_PUBLIC_CODER_INSTRUCTIONS,
            ),
            **(
                {
                    _THREAD_PRESET_HAKU_CLAUDE: ThreadPreset(
                        title="Haku",
                        harness=Harness.CLAUDE,
                        model=haku_preset_model,
                        cwd="/state/workspaces/{session_id}",
                        reasoning_effort="medium",
                        # Mirrors haku.agent.yaml's `system` prose (the same pointer the cloud and
                        # self-hosted managed agents both carry): who reads what, and where the
                        # real instructions live -- deliberately not duplicated here, so this
                        # preset cannot drift from Haku's own run procedure.
                        instructions=(
                            "You are Haku, the operator's tireless background executive "
                            "assistant. Your haku-state checkout -- your memory, your method, "
                            "and your only write surface -- is at haku-state, with git auth "
                            "already in place. It also holds who you are: read AGENTS.md, "
                            "SOUL.md and MEMORY.md at its root, then your run procedure at "
                            "memory/procedures/run.md. Read those, then execute the run "
                            "procedure end to end. Commit and push haku-state as you go."
                        ),
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
                        # At least claude-ai's own reach (actions_staging_policies.py's
                        # EgressBinding for haku-agent binds the same superset at the
                        # ServiceAccount level); listed again here because a preset's
                        # `policies` are what a *launch* is granted, independent of which
                        # caller/ServiceAccount stamps it.
                        policies=[
                            BASIC_POLICY,
                            FORGEJO_HAKU_POLICY,
                            PACKAGES_POLICY,
                            GOOGLE_READONLY_POLICY,
                            GROCY_SF_READONLY_POLICY,
                            HOME_ASSISTANT_READONLY_POLICY,
                            ACTIVITYWATCH_READ_POLICY,
                            AIQUOTA_READ_POLICY,
                            HAKU_MAILBOX_POLICY,
                            COINBASE_POLICY,
                            PLAID_PGWEB_POLICY,
                            GITHUB_CLONE_POLICY,
                            GITHUB_AGENTYDRAGON_AGENT_POLICY,
                            GITHUB_ACTIONS_LOGS_POLICY,
                        ],
                        thread_preset=_THREAD_PRESET_HAKU_CLAUDE,
                        # Shallow clone of haku-state over the in-cluster Forgejo, the way
                        # haku-sandbox-setup.sh clones it for Haku's own sandboxes
                        # (haku/sandbox/image/haku-sandbox-setup.sh): --depth 1
                        # because the box only needs the HEAD checkout, not full history. The
                        # URL's userinfo carries the literal placeholder string as the password
                        # half; git turns that into a Basic Authorization header, and the
                        # `forgejo-haku` EgressPolicy's credentialRef substitutes it for the
                        # `haku` Forgejo account's real password on the way out
                        # (egress_staging_credentials.py) -- the placeholder itself is inert, so
                        # it is safe to embed literally here. ducktape is cloned too, read-only
                        # reference the same way Haku's own sandboxes carry it.
                        bootstrap=_HAKU_BOOTSTRAP,
                    )
                }
                if haku_preset_model is not None
                else {}
            ),
        },
        # Granted to every sandbox before whatever the operator picks: without the model
        # endpoint a sandbox has no agent, so it is not a choice (see this namespace's
        # egress/ directory).
        default_policies=[BASIC_POLICY],
        # The egress proxy's admin port (agentplane/egress `Settings.admin_port`), asked
        # for each sandbox's recent decisions; until the proxy Deployment lands the page
        # shows the rules alone.
        egress_admin_url=f"http://agentplane-egress-admin.{namespace}.svc:8081",
        action_federation=action_federation,
    )
