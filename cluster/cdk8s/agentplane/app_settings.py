"""Shared app settings and presets, parameterized by each environment's model routes and policies."""

from __future__ import annotations

from pathlib import Path

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
from cluster.cdk8s.agentplane.egress import (
    BASIC_POLICY,
    BUILDBUDDY_POLICY,
    GITHUB_ACTIONS_LOGS_POLICY,
    GITHUB_AGENTYDRAGON_AGENT_POLICY,
    GITHUB_CLONE_POLICY,
    INFERENCE_EXPERIMENTS_POLICY,
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
    action_federation: ActionFederationSettings | None = None,
    action_policy_sets: list[str] | None = None,
    sandbox_service_grpc_channel_options: dict[str, int | str] | None = None,
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
        # Grant platform operations and inference experiments independently. The latter
        # is a fleet default, not part of the basic Agentplane platform policy.
        default_egress_policies=[BASIC_POLICY, INFERENCE_EXPERIMENTS_POLICY],
        # The egress proxy's admin port (agentplane/egress `Settings.admin_port`), asked
        # for each sandbox's recent decisions; until the proxy Deployment lands the page
        # shows the rules alone.
        egress_admin_url=f"http://agentplane-egress-admin.{namespace}.svc:8081",
        action_federation=action_federation,
    )
