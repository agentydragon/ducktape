"""Shared app settings and presets, parameterized by each environment's model routes and policies."""

from __future__ import annotations

from collections.abc import Set
from pathlib import Path

from pydantic import AnyHttpUrl

from agentplane.app.action_federation_settings import ActionFederationSettings
from agentplane.app.model_catalog import CentralSamplerChoice, ModelCatalog, ModelOption
from agentplane.app.presets import SandboxPreset, ThreadPreset
from agentplane.app.settings import AppSettingsConfig, FrontendConfig
from agentplane.runner.harness import Harness

# The names of the objects other modules create. `settings()` below grants the public-coder
# preset its baseline policies, so it refers to them; each comes from the module that creates the
# policy answering to it, so a preset names a policy without repeating the string. The Pod execution
# environment a preset launches on is the caller's: `settings()` takes its name.
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


def model_catalog(models: HarnessRoutes, *, central_sampler: CentralSamplerChoice | None = None) -> ModelCatalog:
    return ModelCatalog(
        models=[
            ModelOption(
                model=route.id,
                display_name=_picker_model_name(route),
                reasoning_efforts=_reasoning_efforts(route, models),
            )
            for route in models.all
        ],
        central_sampler=central_sampler,
        harnesses={
            Harness.CLAUDE: [route.id for route in models.claude],
            Harness.CODEX: [route.id for route in models.codex],
            **({Harness.CENTRAL_SAMPLER: [route.id for route in models.codex]} if central_sampler is not None else {}),
        },
    )


def settings(
    *,
    namespace: str,
    sandbox_template: str,
    models: HarnessRoutes,
    thread_preset_codex_model: Route,
    connection_service_url: HttpEndpointUrl,
    action_federation: ActionFederationSettings,
    public_coder_policy_sets: list[str],
    default_policy_sets: Set[str],
    frontend_config: FrontendConfig | None = None,
    central_sampler: CentralSamplerChoice | None = None,
) -> AppSettingsConfig:
    optional_configuration: dict[str, object] = {}
    if frontend_config is not None:
        optional_configuration["frontend_config"] = frontend_config
    return AppSettingsConfig(
        models=model_catalog(models, central_sampler=central_sampler),
        default_policy_sets=default_policy_sets,
        connection_service_url=connection_service_url,
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
                template=sandbox_template,
                policy_sets=[
                    BASIC_POLICY,
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
        **optional_configuration,
    )
