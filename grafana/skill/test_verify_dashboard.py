"""Regression coverage for operator datasource substitution in candidate checks."""

from __future__ import annotations

import runpy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from python.runfiles import Runfiles

VERIFY_DASHBOARD = runpy.run_path(str(Runfiles.CreateOrRaise().root() / "ducktape/grafana/skill/verify_dashboard.py"))


@pytest.mark.parametrize(
    ("source", "resource_name"),
    [
        ({"configMapRef": {"key": "home-network-overview.json"}}, "home-network-overview"),
        ({"json": '{"uid":"home-network-overview"}'}, "home-network-overview"),
        ({"json": '{"uid":"home-network-overview"}'}, "different-resource-name"),
    ],
)
def test_operator_mapping_uses_dashboard_resource_and_datasource_name(
    tmp_path: Path, source: dict[str, Any], resource_name: str
) -> None:
    dashboard_resources = tmp_path / "dashboard.yaml"
    dashboard_resources.write_text(
        yaml.safe_dump_all(
            [
                {
                    "kind": "GrafanaDashboard",
                    "metadata": {"name": resource_name, "namespace": "monitoring"},
                    "spec": {**source, "datasources": [{"inputName": "DS_LOKI", "datasourceName": "Loki"}]},
                }
            ]
        )
    )
    datasource_inputs = VERIFY_DASHBOARD["operator_datasources"](dashboard_resources, "home-network-overview.json")
    assert datasource_inputs == {"DS_LOKI": "Loki"}

    by_name = {"Loki": {"name": "Loki", "type": "loki", "uid": "loki-live-uid"}}
    by_uid = {"loki-live-uid": by_name["Loki"]}
    valid_panel = VERIFY_DASHBOARD["apply_operator_datasources"]({"datasource": "${DS_LOKI}"}, datasource_inputs)
    assert valid_panel["datasource"] == "Loki"
    assert VERIFY_DASHBOARD["datasource_for"](valid_panel, {}, by_name, by_uid) is by_name["Loki"]

    invalid_uid_panel = VERIFY_DASHBOARD["apply_operator_datasources"](
        {"datasource": {"type": "loki", "uid": "${DS_LOKI}"}}, datasource_inputs
    )
    assert invalid_uid_panel["datasource"] == {"type": "loki", "uid": "Loki"}
    with pytest.raises(VERIFY_DASHBOARD["VerificationError"], match="has no live datasource"):
        VERIFY_DASHBOARD["datasource_for"](invalid_uid_panel, {}, by_name, by_uid)

    fixed_uid_panel = {"datasource": {"type": "loki", "uid": "loki-live-uid"}}
    assert VERIFY_DASHBOARD["datasource_for"](fixed_uid_panel, {}, by_name, by_uid) is by_name["Loki"]


def test_operator_mapping_substitutes_arbitrary_input_names_and_preserves_dashboard_variables() -> None:
    dashboard = VERIFY_DASHBOARD["apply_operator_datasources"](
        {"datasource": "${datasource}", "templating": {"query": "${dashboard_var}"}}, {"datasource": "Loki"}
    )
    assert dashboard == {"datasource": "Loki", "templating": {"query": "${dashboard_var}"}}

    with pytest.raises(VERIFY_DASHBOARD["VerificationError"], match="no GrafanaDashboard mapping"):
        VERIFY_DASHBOARD["apply_operator_datasources"]({"datasource": "${DS_UNKNOWN}"}, {"datasource": "Loki"})


def test_query_target_datasource_overrides_panel_datasource() -> None:
    dashboard = VERIFY_DASHBOARD["apply_operator_datasources"](
        {
            "panels": [
                {
                    "id": 1,
                    "title": "Loki target overrides Mimir panel",
                    "type": "timeseries",
                    "datasource": "${DS_MIMIR}",
                    "targets": [{"refId": "A", "expr": "sum(log_entries_total)", "datasource": "${DS_LOKI}"}],
                }
            ]
        },
        {"DS_MIMIR": "Mimir", "DS_LOKI": "Loki"},
    )
    by_name = {
        "Mimir": {"name": "Mimir", "type": "prometheus", "uid": "mimir-live-uid"},
        "Loki": {"name": "Loki", "type": "loki", "uid": "loki-live-uid"},
    }
    by_uid = {item["uid"]: item for item in by_name.values()}
    requests: list[dict[str, Any]] = []

    def post(path: str, json: dict[str, Any]) -> SimpleNamespace:
        assert path == "/api/ds/query"
        requests.append(json)
        return SimpleNamespace(
            status_code=200,
            json=lambda: {
                "results": {"A": {"frames": [{"schema": {"fields": [{"name": "Value"}]}, "data": {"values": [[1.0]]}}]}}
            },
        )

    api = SimpleNamespace(client=SimpleNamespace(post=post))
    VERIFY_DASHBOARD["query_panels"](api, dashboard, by_name, by_uid, 1, 2, [], [])

    assert requests[0]["queries"][0]["datasource"] == {"type": "loki", "uid": "loki-live-uid"}
