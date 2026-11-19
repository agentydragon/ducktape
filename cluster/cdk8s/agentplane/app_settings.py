"""Shared app settings and presets, parameterized by each environment's model routes and policies."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from pydantic import AnyHttpUrl

from agentplane.action_service.orchestration_v1.binding import (
    LaunchPreset,
    PresetLaunch,
    PresetSession,
    PresetSessionSpec,
)
from agentplane.app.action_federation_settings import ActionFederationSettings
from agentplane.app.oidc import OIDCSettings
from agentplane.app.presets import SandboxPreset, ThreadPreset
from agentplane.app.settings import FrontendConfig, Settings
from agentplane.env.runtime.harness import Harness
from agentplane.net.peertls.config import APP_URI, POLICY_CONTROLLER_URI
from agentplane.sandboxes.runtimeconfig.model_catalog import (
    WORKSPACE_ROOT,
    Catalog,
    ModelCatalog,
    ModelOption,
    Provenance,
    TemplateRuntimeConfig,
)

# The names of the objects other modules create. `settings()` below grants the public-coder
# preset its selected baseline and execution policies, so it refers to them; each comes from the
# module that creates the policy answering to it, so a preset names a policy without repeating the
# string. The Pod execution environment a preset launches on is the caller's: `settings()` takes its name.
from cluster.cdk8s.agentplane import (
    egress,
    environment_service,
    history_service,
    notifications,
    policy_controller,
    runner_gateway,
    workload_tls,
)
from cluster.cdk8s.agentplane.egress import (
    BUILDBUDDY_POLICY,
    GITHUB_ACTIONS_LOGS_POLICY,
    GITHUB_AGENTYDRAGON_AGENT_POLICY,
    GITHUB_CLONE_POLICY,
    PACKAGES_POLICY,
    PUBLIC_CODER_VISUALS_POLICY,
)
from cluster.cdk8s.agentplane.model_selections import HarnessRoutes, chat_completions_id
from model_catalog.catalog import Provider, Route
from util.urls import HttpEndpointUrl

_THREAD_PRESET_PUBLIC_CODER_CODEX = "public-coder-codex"
_COMMON_HARNESS_REASONING_EFFORTS = frozenset(("low", "medium", "high"))

# CLIProxyAPI live probe (2026-10-19): 16 one-token `/v1/messages` requests with adaptive
# thinking. Sonnet 4.6 (`low`/`medium`/`high`/`max`) and both Gemini routes
# (`minimal`/`low`/`medium`/`high`) returned HTTP 200 at every route-declared effort. All
# four Opus efforts returned 429 `Individual quota reached`, so those remain unverified.
# Agentplane stores one effort list per model across both harnesses, so expose their common
# values: low, medium, high.
# Follow-up: `gemini-3.8-flash-high` returned HTTP 200 at low, medium, and high through
# `/v1/messages` with adaptive thinking (one 4-token request per effort); minimal is not
# declared for this model by the CLIProxyAPI registry.

DUCKTAPE_PR_INSTRUCTIONS = Path(__file__).with_name("ducktape_pr_instructions.md").read_text(encoding="utf-8").strip()
_PUBLIC_CODER_INSTRUCTIONS = "\n\n".join(
    [
        Path(__file__).with_name("public_coder_instructions.md").read_text(encoding="utf-8").strip(),
        DUCKTAPE_PR_INSTRUCTIONS,
    ]
)

_PICKER_PROVIDER_NAMES = {
    Provider.ANTHROPIC_API: "Anthropic API",
    Provider.CHATGPT: "ChatGPT",
    Provider.ANTIGRAVITY: "Google Antigravity",
    Provider.OLLAMA: "cluster Ollama",
}


def _picker_model_name(route: Route) -> str:
    """Append the serving provider to every Agentplane picker entry."""
    provider = _PICKER_PROVIDER_NAMES[route.upstream.provider]
    return f"{route.display_name} — {provider}"


def _reasoning_efforts(route: Route, models: HarnessRoutes) -> list[str]:
    efforts = route.reasoning_efforts
    if route in models.claude and route in models.codex:
        efforts = tuple(effort for effort in efforts if effort in _COMMON_HARNESS_REASONING_EFFORTS)
    return list(efforts)


def model_catalog(models: HarnessRoutes, *, central_sampler_enabled: bool = False) -> ModelCatalog:
    options = [
        ModelOption(
            model=route.id, display_name=_picker_model_name(route), reasoning_efforts=_reasoning_efforts(route, models)
        )
        for route in models.all
    ]
    harnesses = {
        Harness.CLAUDE: [route.id for route in models.claude],
        Harness.CODEX: [route.id for route in models.codex],
    }
    if central_sampler_enabled:
        sampler_ids = [route.id for route in models.central_sampler]
        if (canary := models.central_sampler_chat_canary) is not None:
            model_id = chat_completions_id(canary)
            options.append(
                ModelOption(
                    model=model_id,
                    display_name=f"{canary.display_name} — TabbyAPI Chat Completions (canary)",
                    reasoning_efforts=[],
                )
            )
            sampler_ids.append(model_id)
        harnesses[Harness.CENTRAL_SAMPLER] = sampler_ids
    return ModelCatalog(models=options, harnesses=harnesses)


def runtime_catalog(
    models: HarnessRoutes,
    *,
    templates: tuple[str, ...],
    workspace_root_enforced_templates: tuple[str, ...] = (),
    central_sampler_enabled: bool = False,
) -> Catalog:
    """Publish runtime choices and workspace guidance only for rendered templates."""
    return Catalog(
        models=model_catalog(models, central_sampler_enabled=central_sampler_enabled),
        templates={
            template: TemplateRuntimeConfig(
                workspace_root=WORKSPACE_ROOT,
                workspace_provenance=Provenance.DEPLOYMENT_CONFIG,
                workspace_root_enforced=template in workspace_root_enforced_templates,
                creates_missing_cwd=True,
            )
            for template in templates
        },
    )


def settings(
    *,
    namespace: str,
    sandbox_template: str,
    models: HarnessRoutes,
    runtime_catalog: Catalog | None = None,
    thread_preset_codex_model: Route,
    connection_service_url: HttpEndpointUrl,
    action_federation: ActionFederationSettings,
    public_coder_policy_sets: list[str],
    basic_policy_set: str,
    app_origin: str,
    oidc_issuer: str,
    electric_url: HttpEndpointUrl | None = None,
    frontend_config: FrontendConfig | None = None,
    central_sampler_enabled: bool = False,
) -> Settings:
    configured = Settings(
        models=(
            runtime_catalog.models
            if runtime_catalog is not None
            else model_catalog(models, central_sampler_enabled=central_sampler_enabled)
        ),
        connection_service_url=connection_service_url,
        # App-owned launch-form presets. The browser expands one into editable concrete
        # template, policy, bootstrap, and SessionSpec fields; neither a Sandbox CR nor a
        # runner receives a preset name.
        thread_presets={
            _THREAD_PRESET_PUBLIC_CODER_CODEX: ThreadPreset(
                title="Public coder / Codex",
                harness=Harness.CODEX,
                model=thread_preset_codex_model.id,
                cwd=f"{WORKSPACE_ROOT}/{{session_id}}",
                reasoning_effort="medium",
                instructions=_PUBLIC_CODER_INSTRUCTIONS,
            )
        },
        sandbox_presets={
            "public-coder": SandboxPreset(
                title="Public coder",
                template=sandbox_template,
                policy_sets=[
                    basic_policy_set,
                    PACKAGES_POLICY,
                    GITHUB_AGENTYDRAGON_AGENT_POLICY,
                    GITHUB_CLONE_POLICY,
                    GITHUB_ACTIONS_LOGS_POLICY,
                    BUILDBUDDY_POLICY,
                    PUBLIC_CODER_VISUALS_POLICY,
                    *public_coder_policy_sets,
                ],
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
        sandbox_namespace=namespace,
        runner_gateway_target=(
            f"{environment_service.environments_service(namespace).fqdn}:{environment_service.environments_service(namespace).pod_port}"
        ),
        session_dispatcher_target=(
            f"{runner_gateway.sessions_service(namespace).fqdn}:{runner_gateway.sessions_service(namespace).port.number}"
        ),
        policy_service_target=policy_controller.tls_target(namespace),
        policy_service_tls=workload_tls.service_client_tls(POLICY_CONTROLLER_URI, client_uri=APP_URI),
        history_service_target=(
            f"{history_service.service(namespace).fqdn}:{history_service.service(namespace).port.number}"
        ),
        notifications_url=AnyHttpUrl(notifications.service(namespace).url),
        listen_address="0.0.0.0:8080",
        electric_url=electric_url,
        oidc=OIDCSettings(issuer=oidc_issuer, public_base_url=app_origin),
    )
    if frontend_config is not None:
        configured.frontend_config = frontend_config
    return configured


def orchestration_presets(
    *, sandbox_presets: Mapping[str, SandboxPreset], thread_presets: Mapping[str, ThreadPreset]
) -> dict[str, LaunchPreset]:
    presets = {}
    for name, sandbox in sandbox_presets.items():
        thread = thread_presets[sandbox.thread_preset]
        spec = PresetSessionSpec(
            harness=thread.harness,
            model=thread.model,
            cwd=thread.cwd,
            reasoning_effort=thread.reasoning_effort or "",
            instructions=thread.instructions,
        )
        presets[name] = LaunchPreset(
            title=sandbox.title,
            launch=PresetLaunch(
                template=sandbox.template,
                policy_sets=sandbox.policy_sets,
                inbox_tags=sandbox.inbox_tags,
                bootstrap=sandbox.bootstrap,
            ),
            create_session=PresetSession(spec=spec, setup_script=thread.setup_script),
        )
    return presets
