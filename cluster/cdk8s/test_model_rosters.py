"""Route identity, selection, and LiteLLM artifact contracts."""

from dataclasses import replace

import pytest
import pytest_bazel

from cluster.cdk8s.agentplane.model_selections import STAGING_APP_MODELS
from cluster.cdk8s.litellm.config import model_entry
from model_catalog.catalog import (
    ANTHROPIC_API_LATEST_ROUTES,
    ANTHROPIC_API_ROUTES,
    ANTHROPIC_SUBSCRIPTION_ROUTES,
    ANTIGRAVITY_GEMINI_38_FLASH_HIGH,
    ANTIGRAVITY_GEMINI_FLASH,
    ANTIGRAVITY_GEMINI_FLASH_LITE,
    ANTIGRAVITY_ROUTES,
    CHATGPT_RESPONSES_ROUTES,
    GEMINI_EMBEDDING_ALIAS,
    GEMINI_EMBEDDING_ROUTES,
    GEMINI_ROUTES,
    GPT6_1_SOL_RESPONSES,
    GPT6_RESPONSES_ROUTES,
    GPT6_SOL_RESPONSES,
    HIDDEN_ALIASES,
    OLLAMA_CHAT_ROUTES,
    OLLAMA_EMBEDDING_ROUTE,
    RUGGED_LOCAL_CANDIDATE_ROUTES,
    RUGGED_LOCAL_EXPERIMENTAL_ROUTES,
    SERVED_ROUTES,
    EmbeddingLimits,
    Model,
    Provider,
    Route,
    RouteAlias,
    TokenLimits,
)
from model_catalog.policies import KEY_MODEL_LANES


def test_catalog_has_unique_ids_and_aliases_reference_served_routes() -> None:
    by_id = {route.id: route for route in SERVED_ROUTES}
    assert len(by_id) == len(SERVED_ROUTES)
    for alias in (*HIDDEN_ALIASES, GEMINI_EMBEDDING_ALIAS):
        assert by_id[alias.target.id] == alias.target
    assert GEMINI_EMBEDDING_ALIAS.target == GEMINI_EMBEDDING_ROUTES[0]
    assert GEMINI_EMBEDDING_ALIAS.id not in {route.id for route in GEMINI_EMBEDDING_ROUTES}


def test_staging_picker_does_not_widen_its_key() -> None:
    admitted = (
        *KEY_MODEL_LANES["gpt6_oai_lane_models"].allowed,
        *KEY_MODEL_LANES["claude_client_models"].allowed,
        *KEY_MODEL_LANES["antigravity_client_models"].allowed,
        *KEY_MODEL_LANES["ollama_chat_client_models"].allowed,
    )
    # Wrapped harness choices remain within their LiteLLM key lanes. Direct API models
    # selected only by the central sampler bypass LLM ingress and use their own connection.
    assert all(route in admitted for route in (*STAGING_APP_MODELS.claude, *STAGING_APP_MODELS.codex))
    central_only = (
        set(STAGING_APP_MODELS.central_sampler) - set(STAGING_APP_MODELS.claude) - set(STAGING_APP_MODELS.codex)
    )
    assert central_only == set(ANTHROPIC_API_LATEST_ROUTES)
    assert not central_only.intersection(admitted)


def test_gpt_6_1_sol_is_available_to_agentplane_codex() -> None:
    route = GPT6_1_SOL_RESPONSES
    expected_codex_efforts = ("low", "medium", "high", "xhigh", "max")

    assert route.id == "chatgpt-sub/oai-responses/gpt-6.1-sol"
    assert route in STAGING_APP_MODELS.codex
    assert GPT6_SOL_RESPONSES not in STAGING_APP_MODELS.codex
    assert route in KEY_MODEL_LANES["gpt6_oai_lane_models"].allowed
    assert all(candidate.reasoning_efforts == expected_codex_efforts for candidate in GPT6_RESPONSES_ROUTES)
    assert model_entry(route)["model_info"] == {
        "mode": "responses",
        "supports_function_calling": True,
        "max_input_tokens": 272_000,
        "max_output_tokens": 128_000,
        "max_tokens": 128_000,
    }


def test_rugged_local_candidates_are_served_and_experiment_only() -> None:
    lane = KEY_MODEL_LANES["cheap_experiments_models"]
    routes = (*RUGGED_LOCAL_CANDIDATE_ROUTES, *RUGGED_LOCAL_EXPERIMENTAL_ROUTES)
    served_rugged_routes = {
        route
        for route in SERVED_ROUTES
        if isinstance(route, Route) and route.upstream.provider == Provider.RUGGED_LOCAL
    }
    allowed_rugged_routes = {
        route for route in lane.allowed if isinstance(route, Route) and route.upstream.provider == Provider.RUGGED_LOCAL
    }
    assert served_rugged_routes == set(routes)
    assert allowed_rugged_routes == set(routes)
    assert not lane.fallbacks
    assert [route.id for route in routes] == [
        "rugged-local/oai-chat/qwen3-4b-instruct-2507-q4-k-m",
        "rugged-local/oai-chat/gemma4-e4b",
        "rugged-local/oai-chat/gemma4-e2b-it-qat-long-128k-experimental",
    ]
    assert [route.upstream_model for route in routes] == [
        "rugged-qwen3-4b-instruct-2507-q4_K_M",
        "rugged-gemma4-e4b",
        "rugged-gemma4-e2b-it-qat-long-128k-b128",
    ]
    assert all(
        route.model.limits == TokenLimits(max_input_tokens=28_672, max_output_tokens=4_096)
        for route in RUGGED_LOCAL_CANDIDATE_ROUTES
    )
    [route] = RUGGED_LOCAL_EXPERIMENTAL_ROUTES
    assert route.model.limits == TokenLimits(max_input_tokens=126_976, max_output_tokens=4_096)
    assert all(route.publish_limits for route in routes)


@pytest.mark.parametrize("context_kib", [256, 512, 1024])
def test_large_gpt_oss_variants_only_serve_native_requests(context_kib: int) -> None:
    [native] = [
        route
        for route in OLLAMA_CHAT_ROUTES
        if route.upstream_model == "gpt-oss:20b" and route.num_ctx == context_kib * 1024
    ]
    # Parking the misleading wire must neither leave it served nor drop the native
    # variant's explicit allocation. Other models/wires are outside this pause.
    assert [route for route in SERVED_ROUTES if isinstance(route, Route) and route.model == native.model] == [native]
    assert model_entry(native)["litellm_params"]["extra_body"] == {"options": {"num_ctx": context_kib * 1024}}


def test_equal_model_slugs_do_not_collapse_account_routes() -> None:
    direct = ANTHROPIC_API_ROUTES[0]
    # Construct a shared slug with unknown subscription limits: production rosters
    # can advance independently, but account identity must still prevent leakage.
    subscription = replace(ANTHROPIC_SUBSCRIPTION_ROUTES[0], model=Model(direct.model.id), publish_limits=False)
    assert subscription.model.id == direct.model.id
    assert subscription.model.limits is None
    assert direct.model.limits is not None
    assert subscription.id != direct.id
    assert model_entry(subscription)["litellm_params"]["api_key"] != model_entry(direct)["litellm_params"]["api_key"]
    assert subscription.reasoning_efforts
    assert not direct.reasoning_efforts


def test_latest_direct_anthropic_routes_declare_model_specific_api_capabilities() -> None:
    assert [route.model.id for route in ANTHROPIC_API_LATEST_ROUTES] == [
        "claude-opus-5-5",
        "claude-sonnet-5-5",
        "claude-haiku-5-5",
        "claude-fable-5-1",
    ]
    for route in ANTHROPIC_API_LATEST_ROUTES:
        assert route.upstream.provider == Provider.ANTHROPIC_API
        assert route.reasoning_efforts == ("low", "medium", "high", "xhigh", "max")
        assert route.supports_function_calling is True
        assert route.model.limits == TokenLimits(max_input_tokens=1_000_000, max_output_tokens=128_000)
        assert route in STAGING_APP_MODELS.central_sampler
        assert route not in STAGING_APP_MODELS.claude
        assert route not in STAGING_APP_MODELS.codex
        assert model_entry(route)["model_info"]["supports_function_calling"] is True


def test_antigravity_google_routes_are_served_and_selected_for_both_harnesses() -> None:
    routes = (ANTIGRAVITY_GEMINI_FLASH, ANTIGRAVITY_GEMINI_FLASH_LITE, ANTIGRAVITY_GEMINI_38_FLASH_HIGH)
    assert [route.model.id for route in routes] == ["gemini-3-flash", "gemini-3.5-flash-lite", "gemini-3.8-flash-high"]
    assert all(route.reasoning_efforts == ("minimal", "low", "medium", "high") for route in routes[:2])
    assert ANTIGRAVITY_GEMINI_38_FLASH_HIGH.reasoning_efforts == ("low", "medium", "high")
    assert ANTIGRAVITY_GEMINI_38_FLASH_HIGH.model.reasoning is True
    assert all(route in ANTIGRAVITY_ROUTES for route in routes)
    assert all(route in STAGING_APP_MODELS.claude and route in STAGING_APP_MODELS.codex for route in routes)
    assert all(route in KEY_MODEL_LANES["antigravity_client_models"].allowed for route in routes)
    assert all(model_entry(route)["model_name"] == route.id for route in routes)
    assert [route.model.limits for route in routes] == [
        TokenLimits(max_input_tokens=1_048_576, max_output_tokens=65_536),
        TokenLimits(max_input_tokens=1_048_576, max_output_tokens=65_535),
        TokenLimits(max_input_tokens=1_048_576, max_output_tokens=65_536),
    ]
    for route in routes:
        limits = route.model.limits
        assert isinstance(limits, TokenLimits)
        assert model_entry(route)["model_info"]["max_output_tokens"] == limits.max_output_tokens
    assert not {"gemini-pro-agent", "gemini-3.1-pro-low"} & {route.model.id for route in ANTIGRAVITY_ROUTES}


def test_flash_lite_keeps_distinct_google_account_routes() -> None:
    direct = [route for route in GEMINI_ROUTES if route.model.id == "gemini-3.5-flash-lite"]
    assert [route.id for route in direct] == ["google/goog-generate/gemini-3.5-flash-lite"]
    assert direct[0].id != ANTIGRAVITY_GEMINI_FLASH_LITE.id
    assert ANTIGRAVITY_GEMINI_FLASH_LITE.id == "antigravity/ant-messages/gemini-3.5-flash-lite"
    assert direct[0].model.limits == TokenLimits(max_input_tokens=1_048_576, max_output_tokens=65_536)
    assert ANTIGRAVITY_GEMINI_FLASH_LITE.model.limits == TokenLimits(
        max_input_tokens=1_048_576, max_output_tokens=65_535
    )


@pytest.mark.parametrize("publish_limits", [False, True])
def test_unknown_limits_are_not_invented_or_published(publish_limits: bool) -> None:
    route = replace(CHATGPT_RESPONSES_ROUTES[0], model=Model("unknown-model"), publish_limits=publish_limits)
    if publish_limits:
        with pytest.raises(ValueError, match="unknown limits"):
            model_entry(route)
    else:
        assert not {"max_input_tokens", "max_output_tokens", "max_tokens"} & model_entry(route)["model_info"].keys()


@pytest.mark.parametrize("entry", [*GEMINI_EMBEDDING_ROUTES, GEMINI_EMBEDDING_ALIAS, OLLAMA_EMBEDDING_ROUTE])
def test_embedding_input_aliases_follow_the_declaration_without_output_limits(entry: Route | RouteAlias) -> None:
    original = model_entry(entry)
    if entry == OLLAMA_EMBEDDING_ROUTE:
        assert original["litellm_params"]["truncate"] is False
        assert "extra_body" not in original["litellm_params"]
        assert "options" not in original["litellm_params"]
    else:
        assert "truncate" not in original["litellm_params"]
    route = entry.target if isinstance(entry, RouteAlias) else entry
    changed = replace(route, model=replace(route.model, limits=EmbeddingLimits(max_input_tokens=12345)))
    projected = model_entry(replace(entry, target=changed) if isinstance(entry, RouteAlias) else changed)
    assert projected["model_name"] == original["model_name"]
    assert projected["litellm_params"] == original["litellm_params"]
    assert projected["model_info"] == {"mode": "embedding", "max_input_tokens": 12345, "max_tokens": 12345}


def test_published_limit_kinds_must_match_the_route_mode() -> None:
    chat = CHATGPT_RESPONSES_ROUTES[0]
    embedding = GEMINI_EMBEDDING_ROUTES[0]
    with pytest.raises(ValueError, match="embedding limits require an embedding route"):
        model_entry(replace(chat, model=embedding.model))
    with pytest.raises(ValueError, match="generative limits require a chat or responses route"):
        model_entry(replace(embedding, model=chat.model))


def test_missing_display_name_is_not_prettified_from_a_slug() -> None:
    route = next(route for route in SERVED_ROUTES if isinstance(route, Route))
    with pytest.raises(ValueError, match="no display name"):
        _ = replace(route, model=Model("unknown-model")).display_name


if __name__ == "__main__":
    pytest_bazel.main()
