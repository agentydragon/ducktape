"""Route identity, selection, and LiteLLM artifact contracts."""

from dataclasses import replace

import pytest
import pytest_bazel

from cluster.cdk8s.litellm.config import model_entry
from cluster.cdk8s.model_selections import STAGING_APP_MODELS, TESTING_APP_MODELS
from model_catalog.catalog import (
    ANTHROPIC_API_ROUTES,
    ANTHROPIC_SUBSCRIPTION_ROUTES,
    ANTIGRAVITY_ROUTES,
    CHATGPT_RESPONSES_ROUTES,
    GEMINI_EMBEDDING_ALIAS,
    GEMINI_EMBEDDING_ROUTES,
    GEMINI_ROUTES,
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


def test_testing_picker_does_not_widen_its_key() -> None:
    admitted = KEY_MODEL_LANES["cheap_experiments_models"].allowed
    assert all(route in admitted for route in TESTING_APP_MODELS.all)


def test_staging_picker_does_not_widen_its_key() -> None:
    admitted = (
        *KEY_MODEL_LANES["gpt6_oai_lane_models"].allowed,
        *KEY_MODEL_LANES["claude_client_models"].allowed,
        *KEY_MODEL_LANES["antigravity_client_models"].allowed,
        *KEY_MODEL_LANES["ollama_chat_client_models"].allowed,
    )
    assert all(route in admitted for route in STAGING_APP_MODELS.all)


def test_picker_is_narrower_than_ollama_key() -> None:
    assert all(route in OLLAMA_CHAT_ROUTES for route in TESTING_APP_MODELS.codex[1:])
    assert any(route not in TESTING_APP_MODELS.all for route in OLLAMA_CHAT_ROUTES)


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


def test_same_slug_on_different_accounts_keeps_distinct_limits() -> None:
    shared_slugs = [
        (google, antigravity)
        for google in GEMINI_ROUTES
        for antigravity in ANTIGRAVITY_ROUTES
        if google.model.id == antigravity.model.id
    ]
    assert shared_slugs
    for google, antigravity in shared_slugs:
        assert google.model.limits is not None
        assert antigravity.model.limits is not None
        assert google.model.limits != antigravity.model.limits
        assert (
            model_entry(google)["model_info"]["max_output_tokens"]
            != model_entry(antigravity)["model_info"]["max_output_tokens"]
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
