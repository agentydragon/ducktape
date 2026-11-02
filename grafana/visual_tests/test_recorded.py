"""Recorded dashboard responses and variable setup."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_bazel

from grafana.visual_tests.recorded import recorded_result, replay_query, validate_fixture
from grafana.visual_tests.visual_harness import _prepare_render_fixture


def test_recorded_results_and_repeated_panels() -> None:
    result = {"frames": [{"schema": {"fields": [{"name": "Value", "type": "number"}]}, "data": {"values": [[12, 20]]}}]}
    fixture = {"panels": {"32": {"A": result}}}
    dashboard = {"panels": [{"id": 32, "targets": [{"refId": "A", "expr": 'metric{node=~"$node"}'}]}]}
    assert recorded_result({"refId": "A", "requestId": "32A"}, fixture, dashboard) == result
    assert (
        recorded_result({"refId": "A", "requestId": "900A", "expr": 'metric{node=~"node-a"}'}, fixture, dashboard)
        == result
    )
    with pytest.raises(KeyError, match="no unambiguous recorded result"):
        recorded_result({"refId": "A", "requestId": "900A", "expr": "other_metric"}, fixture, dashboard)


def test_repeated_panel_uses_recorded_variant() -> None:
    first = {"frames": [{"data": {"values": [[1]]}}]}
    second = {"frames": [{"data": {"values": [[2]]}}]}
    fixture = {"panels": {"3": {"A": {"variant_variable": "host", "variants": {"a": first, "b": second}}}}}
    dashboard = {"panels": [{"id": 3, "targets": [{"refId": "A", "expr": 'metric{host="$host"}'}]}]}
    assert (
        recorded_result({"refId": "A", "requestId": "900A", "expr": 'metric{host="b"}'}, fixture, dashboard) == second
    )
    with pytest.raises(KeyError, match="no recorded repeated-panel variant"):
        recorded_result({"refId": "A", "requestId": "3A", "expr": 'metric{host="c"}'}, fixture, dashboard)


@pytest.mark.asyncio
async def test_missing_result_fails_instead_of_generating_data() -> None:
    route = MagicMock()
    route.request.post_data_json = {"queries": [{"refId": "A", "requestId": "10A"}]}
    route.fulfill = AsyncMock()
    seen = asyncio.Event()
    missing: list[str] = []
    await replay_query(route, seen, {"panels": {}}, {"panels": []}, missing)
    assert route.fulfill.await_args.kwargs["status"] == 400
    assert missing
    assert seen.is_set()


def test_query_variables_require_fixture_choices() -> None:
    dashboard = {"templating": {"list": [{"name": "rack", "type": "query"}]}}
    with pytest.raises(ValueError, match="missing recorded choices"):
        _prepare_render_fixture(dashboard, {})
    _prepare_render_fixture(dashboard, {"rack": ["east", "west"]})
    assert dashboard["templating"]["list"][0]["query"] == "east,west"


def test_fixture_covers_lazy_and_repeated_queries() -> None:
    dashboard = {
        "templating": {"list": [{"name": "host", "type": "query"}]},
        "panels": [{"id": 7, "targets": [{"refId": "A"}, {"refId": "B"}]}],
    }
    fixture: dict[str, Any] = {"variables": {"host": ["a", "b"]}, "panels": {"7": {"A": {"frames": []}}}}
    with pytest.raises(ValueError, match="panel 7, query B"):
        validate_fixture(dashboard, fixture)
    fixture["panels"]["7"]["B"] = {"variant_variable": "host", "variants": {"a": {"frames": []}}}
    with pytest.raises(ValueError, match="missing recorded variants"):
        validate_fixture(dashboard, fixture)
    fixture["panels"]["7"]["B"]["variants"]["b"] = {"frames": []}
    validate_fixture(dashboard, fixture)


if __name__ == "__main__":
    pytest_bazel.main()
