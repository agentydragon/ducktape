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
from model_catalog.catalog import GPT6_LUNA_RESPONSES, TABBYAPI_QWEN_STOCK


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


if __name__ == "__main__":
    pytest_bazel.main()
