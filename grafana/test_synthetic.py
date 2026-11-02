"""Fixture-driven dashboard response and variable setup."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_bazel

from grafana.synthetic import synthetic_query
from grafana.visual_harness import _prepare_render_fixture


@pytest.mark.asyncio
async def test_fixture_labels_and_panel_data() -> None:
    route = MagicMock()
    route.request.post_data_json = {
        "queries": [
            {"refId": "A", "requestId": "10A", "format": "table", "expr": "sum by (rack) (metric)"},
            {"refId": "B", "requestId": "11B"},
            {"refId": "C", "requestId": "12C", "expr": "metric", "legendFormat": "{{rack}}"},
        ]
    }
    route.fulfill = AsyncMock()
    seen = asyncio.Event()
    await synthetic_query(
        route,
        seen,
        {"labels": {"rack": ["east", "west"], "region": ["one", "two"]}, "panels": {"11": {"series": ["queue"]}}},
        set(),
    )
    results = route.fulfill.await_args.kwargs["json"]["results"]
    table = results["A"]["frames"]
    assert [frame["schema"]["fields"][1]["labels"] for frame in table] == [{"rack": "east"}, {"rack": "west"}]
    assert results["B"]["frames"][0]["schema"]["fields"][1]["labels"] == {"series": "queue"}
    assert results["C"]["frames"][0]["schema"]["fields"][1]["labels"] == {"series": "example-a", "rack": "east"}
    assert seen.is_set()


def test_query_variables_use_fixture_choices() -> None:
    dashboard = {"templating": {"list": [{"name": "rack", "type": "query"}, {"name": "unchanged", "type": "custom"}]}}
    _prepare_render_fixture(dashboard, {"rack": ["east", "west"]})
    variables = dashboard["templating"]["list"]
    assert variables[0]["type"] == "custom"
    assert variables[0]["query"] == "east,west"
    assert variables[1] == {"name": "unchanged", "type": "custom"}


if __name__ == "__main__":
    pytest_bazel.main()
