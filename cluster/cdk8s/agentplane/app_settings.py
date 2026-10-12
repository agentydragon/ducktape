"""Shared app settings and presets, parameterized by each environment's model routes and policies."""

from __future__ import annotations

from pathlib import Path

from pydantic import AnyHttpUrl

from agentplane.app.action_federation_settings import ActionFederationSettings
from agentplane.app.model_catalog import ModelCatalog, ModelOption
from agentplane.app.presets import SandboxPreset, ThreadPreset
from agentplane.app.settings import AppSettingsConfig
from agentplane.runner.harness import Harness
from agentplane.sandbox_service.kubernetes_grants import KubernetesGrant

# The names of the objects other modules create. `settings()` below grants the public-coder
# preset its baseline policies, so it refers to them; each comes from the module that creates the
# policy answering to it, which is what lets either environment's preset name the same policy
# without repeating the string.
from cluster.cdk8s import agent_access_profiles
from cluster.cdk8s.agentplane import egress
from cluster.cdk8s.agentplane.egress import (
    BASIC_POLICY,
    BUILDBUDDY_POLICY,
    GITHUB_ACTIONS_LOGS_POLICY,
    GITHUB_AGENTYDRAGON_AGENT_POLICY,
    GITHUB_CLONE_POLICY,
    PACKAGES_POLICY,
    PUBLIC_CODER_VISUALS_POLICY,
)
from cluster.cdk8s.model_selections import HarnessRoutes
from model_catalog.catalog import Route

_THREAD_PRESET_PUBLIC_CODER_CODEX = "public-coder-codex"

DUCKTAPE_PR_INSTRUCTIONS = Path(__file__).with_name("ducktape_pr_instructions.md").read_text(encoding="utf-8").strip()
_PUBLIC_CODER_INSTRUCTIONS = "\n\n".join(
    [
        Path(__file__).with_name("public_coder_instructions.md").read_text(encoding="utf-8").strip(),
        DUCKTAPE_PR_INSTRUCTIONS,
    ]
)


def settings(
    *,
    namespace: str,
    models: HarnessRoutes,
    thread_preset_codex_model: Route,
    action_federation: ActionFederationSettings,
    action_policy_sets: list[str] | None = None,
    kubernetes_grants: dict[str, KubernetesGrant] | None = None,
) -> AppSettingsConfig:
    return AppSettingsConfig(
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
            )
        },
        sandbox_presets={
            "public-coder": SandboxPreset(
                title="Public coder",
                template="runner",
                egress_policies=[
                    BASIC_POLICY,
                    PACKAGES_POLICY,
                    GITHUB_AGENTYDRAGON_AGENT_POLICY,
                    GITHUB_CLONE_POLICY,
                    GITHUB_ACTIONS_LOGS_POLICY,
                    BUILDBUDDY_POLICY,
                    PUBLIC_CODER_VISUALS_POLICY,
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
            )
        },
        # Asked for each sandbox's recent decisions; the page shows the rules alone when it does not answer.
        egress_admin_url=AnyHttpUrl(egress.admin(namespace).url),
        action_federation=action_federation,
    )


def on_policy_sets(cfg: AppSettingsConfig) -> AppSettingsConfig:
    """The same settings where Sandboxes are Agentplane's own kind, which takes `policy_sets` alone.

    Each egress policy and Action policy set has a PolicySet of its name (`egress_policies.py`,
    `actions_staging_policies.py`); a preset's Kubernetes grants become the sets that cover them.
    """
    # `model_copy` is typed `Any` on this settings class.
    converted: AppSettingsConfig = cfg.model_copy(
        update={
            "own_sandboxes": True,
            "sandbox_presets": {
                name: SandboxPreset.model_validate(
                    {
                        **preset.model_dump(
                            exclude_unset=True, exclude={"egress_policies", "action_policy_sets", "kubernetes_grants"}
                        ),
                        "policy_sets": [
                            *preset.egress_policies,
                            *preset.action_policy_sets,
                            *agent_access_profiles.covering_policy_sets(preset.kubernetes_grants),
                        ],
                    }
                )
                for name, preset in cfg.sandbox_presets.items()
            },
        }
    )
    return converted
