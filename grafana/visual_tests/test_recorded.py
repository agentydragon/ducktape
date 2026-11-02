"""Recorded dashboard responses and variable setup."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_bazel

from grafana.visual_tests.recorded import (
    query_fingerprint,
    recorded_result,
    replay_query,
    validate_fixture,
    variable_fingerprint,
)
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
    dashboard: dict[str, Any] = {
        "templating": {"list": [{"name": "host", "type": "query"}]},
        "panels": [{"id": 7, "targets": [{"refId": "A"}, {"refId": "B"}]}],
    }
    panel = dashboard["panels"][0]
    fixture: dict[str, Any] = {
        "variables": {"host": ["a", "b"]},
        "panels": {"7": {"A": {"frames": []}}},
        "query_fingerprints": {f"7/{target['refId']}": query_fingerprint(panel, target) for target in panel["targets"]},
        "variable_fingerprints": {"host": variable_fingerprint(dashboard["templating"]["list"][0])},
        "empty_reasons": {"7/A": "No samples in the captured window."},
    }
    with pytest.raises(ValueError, match="panel 7, query B"):
        validate_fixture(dashboard, fixture)
    fixture["panels"]["7"]["B"] = {"variant_variable": "host", "variants": {"a": {"frames": []}}}
    with pytest.raises(ValueError, match="missing recorded variants"):
        validate_fixture(dashboard, fixture)
    fixture["panels"]["7"]["B"]["variants"]["b"] = {"frames": []}
    fixture["empty_reasons"].update({"7/B/a": "No samples for a.", "7/B/b": "No samples for b."})
    validate_fixture(dashboard, fixture)


def test_changed_query_variable_requires_fixture_review() -> None:
    variable = {"name": "node", "type": "query", "query": "label_values(up, instance)"}
    dashboard = {"templating": {"list": [variable]}, "panels": []}
    fixture = {
        "variables": {"node": ["node-a"]},
        "panels": {},
        "variable_fingerprints": {"node": variable_fingerprint(variable)},
    }
    validate_fixture(dashboard, fixture)
    variable["query"] = "label_values(other_metric, instance)"
    with pytest.raises(ValueError, match="stale or missing query-variable fingerprint"):
        validate_fixture(dashboard, fixture)


def test_changed_query_requires_fixture_review_even_with_same_panel_and_ref() -> None:
    panel: dict[str, Any] = {"id": 1, "datasource": "Mimir", "targets": [{"refId": "A", "expr": "rate(original[5m])"}]}
    dashboard = {"panels": [panel]}
    fixture = {
        "panels": {"1": {"A": {"frames": [{"data": {"values": [[1]]}}]}}},
        "query_fingerprints": {"1/A": query_fingerprint(panel, panel["targets"][0])},
    }
    validate_fixture(dashboard, fixture)
    panel["targets"][0]["expr"] = "rate(changed[5m])"
    with pytest.raises(ValueError, match="stale or missing query fingerprint"):
        validate_fixture(dashboard, fixture)
    panel["targets"][0]["expr"] = "rate(original[5m])"
    panel["datasource"] = "Other Mimir"
    with pytest.raises(ValueError, match="stale or missing query fingerprint"):
        validate_fixture(dashboard, fixture)


def test_empty_response_requires_reason() -> None:
    target = {"refId": "A", "rawSql": "SELECT value FROM readings"}
    panel = {"id": 2, "targets": [target]}
    fixture = {"panels": {"2": {"A": {"frames": []}}}, "query_fingerprints": {"2/A": query_fingerprint(panel, target)}}
    with pytest.raises(ValueError, match="missing empty-result reason"):
        validate_fixture({"panels": [panel]}, fixture)
    fixture["empty_reasons"] = {"2/A": "No readings in the captured window."}
    validate_fixture({"panels": [panel]}, fixture)


def test_corrected_capture_keeps_source_query_fingerprint() -> None:
    target = {"refId": "A", "rawSql": "SELECT value FROM readings ORDER BY time DESC"}
    panel = {"id": 3, "targets": [target]}
    fixture = {
        "panels": {"3": {"A": {"frames": [{"data": {"values": [[1]]}}]}}},
        "query_fingerprints": {"3/A": query_fingerprint(panel, target)},
        "capture_corrections": {
            "3/A": {
                "query": "SELECT value FROM readings ORDER BY time ASC",
                "reason": "The captured table needed chronological rows.",
            }
        },
    }
    validate_fixture({"panels": [panel]}, fixture)
    target["rawSql"] = "SELECT value FROM other_readings ORDER BY time DESC"
    with pytest.raises(ValueError, match="stale or missing query fingerprint"):
        validate_fixture({"panels": [panel]}, fixture)


if __name__ == "__main__":
    pytest_bazel.main()
