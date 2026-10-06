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
    SERVED_ROUTES,
    Model,
    Route,
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


def test_equal_model_slugs_do_not_collapse_account_routes() -> None:
    subscription, direct = ANTHROPIC_SUBSCRIPTION_ROUTES[0], ANTHROPIC_API_ROUTES[0]
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
        assert antigravity.model.limits is None


@pytest.mark.parametrize("publish_limits", [False, True])
def test_unknown_limits_are_not_invented_or_published(publish_limits: bool) -> None:
    route = replace(CHATGPT_RESPONSES_ROUTES[0], model=Model("unknown-model"), publish_limits=publish_limits)
    if publish_limits:
        with pytest.raises(ValueError, match="unknown limits"):
            model_entry(route)
    else:
        assert not {"max_input_tokens", "max_output_tokens", "max_tokens"} & model_entry(route)["model_info"].keys()


def test_missing_display_name_is_not_prettified_from_a_slug() -> None:
    route = next(route for route in SERVED_ROUTES if isinstance(route, Route))
    with pytest.raises(ValueError, match="no display name"):
        _ = replace(route, model=Model("unknown-model")).display_name


if __name__ == "__main__":
    pytest_bazel.main()
