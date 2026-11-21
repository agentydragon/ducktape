"""Only Claude's selected, explicitly configured overrides become ingress metadata."""

import pytest_bazel

from agentplane.env.runtime.harness import Harness
from cluster.cdk8s.agentplane.app_settings import model_catalog
from cluster.cdk8s.agentplane.model_selections import (
    RUNNER_CONTEXT_OVERRIDES,
    STAGING_APP_MODELS,
    HarnessRoutes,
    model_configs,
)
from model_catalog.catalog import ANTHROPIC_API_LATEST_ROUTES, GPT6_LUNA_RESPONSES, TABBYAPI_QWEN_STOCK


def test_ingress_serves_only_claude_routes_with_an_override() -> None:
    route = TABBYAPI_QWEN_STOCK
    [window] = model_configs(HarnessRoutes(claude=(route,), codex=()))
    assert window.model == route.id
    assert window.total_context_budget_tokens == RUNNER_CONTEXT_OVERRIDES[route]
    # GPT-6 has known metadata, but no configured runner override.
    assert GPT6_LUNA_RESPONSES.model.limits is not None
    assert model_configs(HarnessRoutes(claude=(GPT6_LUNA_RESPONSES,), codex=())) == []


def test_codex_overrides_are_catalog_entries_not_ingress_model_configs() -> None:
    route = TABBYAPI_QWEN_STOCK
    assert route in RUNNER_CONTEXT_OVERRIDES
    assert model_configs(HarnessRoutes(claude=(), codex=(route,))) == []


def test_central_sampler_selection_is_opt_in() -> None:
    catalog = model_catalog(STAGING_APP_MODELS)
    assert Harness.CENTRAL_SAMPLER not in catalog.harnesses


def test_local_picker_matches_current_serving() -> None:
    catalog = model_catalog(STAGING_APP_MODELS, central_sampler_enabled=True)
    options = {option.model: option.display_name for option in catalog.models}
    qwen_id = TABBYAPI_QWEN_STOCK.id
    assert options[qwen_id] == "Qwen3.8 Flash-Next EXL3 2.68 bpw (256K) — TabbyAPI"
    assert options[f"{qwen_id}-chat-completions"] == (
        "Qwen3.8 Flash-Next EXL3 2.68 bpw (256K) — TabbyAPI Chat Completions"
    )
    assert all(not model.startswith("ollama/") for model in options)


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
