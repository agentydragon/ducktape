"""Only Claude's selected, explicitly configured overrides become ingress metadata."""

import pytest_bazel

from agentplane.runner.harness import Harness
from cluster.cdk8s.agentplane.app_settings import model_catalog
from cluster.cdk8s.agentplane.model_selections import (
    RUNNER_CONTEXT_OVERRIDES,
    STAGING_APP_MODELS,
    HarnessRoutes,
    model_configs,
)
from model_catalog.catalog import (
    ANTHROPIC_API_LATEST_ROUTES,
    GPT6_LUNA_RESPONSES,
    OLLAMA_QWEN_IQ4XS_128K,
    OLLAMA_QWEN_IQ4XS_256K,
)


def test_ingress_serves_only_claude_routes_with_an_override() -> None:
    route = OLLAMA_QWEN_IQ4XS_128K.native
    [window] = model_configs(HarnessRoutes(claude=(route,), codex=()))
    assert window.model == route.id
    assert window.total_context_budget_tokens == RUNNER_CONTEXT_OVERRIDES[route]
    # GPT-6 has known metadata, but no configured runner override.
    assert GPT6_LUNA_RESPONSES.model.limits is not None
    assert model_configs(HarnessRoutes(claude=(GPT6_LUNA_RESPONSES,), codex=())) == []


def test_codex_overrides_are_catalog_entries_not_ingress_model_configs() -> None:
    route = OLLAMA_QWEN_IQ4XS_256K.openai
    assert route in RUNNER_CONTEXT_OVERRIDES
    assert model_configs(HarnessRoutes(claude=(), codex=(route,))) == []


def test_direct_anthropic_api_models_are_only_selected_for_the_central_sampler() -> None:
    routes = set(ANTHROPIC_API_LATEST_ROUTES)
    assert routes.issubset(STAGING_APP_MODELS.central_sampler)
    assert not routes.intersection(STAGING_APP_MODELS.claude)
    assert not routes.intersection(STAGING_APP_MODELS.codex)

    catalog = model_catalog(STAGING_APP_MODELS, central_sampler_enabled=True)
    assert {route.id for route in routes}.issubset(catalog.harnesses[Harness.CENTRAL_SAMPLER])
    assert not {route.id for route in routes}.intersection(catalog.harnesses[Harness.CODEX])
    assert not {route.id for route in routes}.intersection(catalog.harnesses[Harness.CLAUDE])
    assert {model.model for model in catalog.models}.issuperset(route.id for route in routes)


if __name__ == "__main__":
    pytest_bazel.main()
