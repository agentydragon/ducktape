"""Lane fallbacks cannot expand authorization beyond served, allowed routes."""

from dataclasses import replace

import pytest

from model_catalog.catalog import GPT6_ASTRA_MESSAGES, GPT6_LUNA_MESSAGES
from model_catalog.policies import ModelLaneRoutes


def test_lane_cannot_authorize_an_unserved_route() -> None:
    route = replace(GPT6_LUNA_MESSAGES, model=replace(GPT6_LUNA_MESSAGES.model, id="not-served"))
    with pytest.raises(ValueError, match="unserved routes"):
        ModelLaneRoutes(allowed=(route,))


def test_fallback_does_not_grant_access() -> None:
    with pytest.raises(ValueError, match="fallback routes outside its allowlist"):
        ModelLaneRoutes(allowed=(GPT6_LUNA_MESSAGES,), fallbacks=(GPT6_ASTRA_MESSAGES,))
