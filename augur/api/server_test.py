"""Smoke the generic Augur server backend."""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any, cast

import pytest
import pytest_bazel

from util.bazel.runfiles import get_required_path
from util.net import pick_free_port
from util.testing.undeclared_outputs import undeclared_outputs_dir


@pytest.fixture(scope="module", params=("polars", "numba"), ids=("polars", "numba"))
def server_url(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    engine = cast(str, request.param)
    tmp_path = tmp_path_factory.mktemp(f"augur_server_{engine}")
    out = undeclared_outputs_dir()
    server_log = (out / f"augur-server-{engine}.log").open("w")
    port = pick_free_port("127.0.0.1")
    server = subprocess.Popen(
        [
            str(get_required_path("_main/augur/api/server")),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--config",
            str(get_required_path("_main/augur/api/testdata/config.yaml")),
            "--api-only",
        ],
        env={
            **os.environ,
            "HOME": str(tmp_path / "home"),
            "MPLCONFIGDIR": str(tmp_path / "matplotlib"),
            "PYTHONUNBUFFERED": "1",
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
            "AUGUR_SIM_ENGINE": engine,
        },
        stdout=server_log,
        stderr=server_log,
    )
    origin = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    try:
        while time.monotonic() < deadline:
            if server.poll() is not None:
                raise RuntimeError(f"Augur server exited early with code {server.returncode}; see {server_log.name}")
            try:
                with urllib.request.urlopen(f"{origin}/healthz", timeout=1) as response:
                    if response.status == 200 and response.read().decode() == "ok\n":
                        break
            except (OSError, urllib.error.URLError):
                time.sleep(0.25)
        else:
            raise RuntimeError(f"Augur server did not start within 30s; see {server_log.name}")
        yield origin
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=10)
        server_log.close()


def _post_json(origin: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{origin}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=240) as response:
        assert response.status == 200
        assert "application/json" in response.headers["content-type"]
        return cast(dict[str, Any], json.loads(response.read().decode()))


def _get_json(origin: str, path: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"{origin}{path}", timeout=15) as response:
        assert response.status == 200
        assert "application/json" in response.headers["content-type"]
        return cast(dict[str, Any], json.loads(response.read().decode()))


def _sum(values: list[float | int]) -> float:
    return float(sum(values))


def _max(values: list[float | int]) -> float:
    return float(max(values))


def _min(values: list[float | int]) -> float:
    return float(min(values))


def test_backend_server_runs_joint_scenario_set_and_materializes_graph_tables(server_url: str) -> None:
    scenario_run = _post_json(
        server_url,
        "/api/scenario_sets/run",
        {
            "scenario_set_id": "backend_smoke",
            "title": "Backend smoke",
            "sampling_request": {"exogenous_model_id": "simple", "rollout_count": 3, "horizon_months": 3, "seed": 11},
            "scenarios": [
                {
                    "scenario_id": "sp500_spend",
                    "label": "SP500 spend",
                    "actors": [{"actor_id": "agent_a", "label": "Agent A", "role": "primary_owner"}],
                    "initial_balance_sheet": {
                        "accounts": [
                            {
                                "account_id": "checking",
                                "account_type": "checking",
                                "owner_actor_id": "agent_a",
                                "balance_usd": 0.0,
                            }
                        ],
                        "assets": [
                            {
                                "asset_id": "wealthfront_sp500",
                                "asset_type": "generic_sp500_stock",
                                "owner_actor_id": "agent_a",
                                "value_usd": 1000.0,
                                "cost_basis_usd": 700.0,
                            }
                        ],
                    },
                    "policies": [
                        {
                            "policy_id": "monthly_spend",
                            "policy_type": "monthly_spend",
                            "actor_id": "agent_a",
                            "monthly_spend_usd": 100.0,
                        },
                        {
                            "policy_id": "sell_sp500",
                            "policy_type": "checking_floor_sell_public_stock",
                            "actor_id": "agent_a",
                            "floor_usd": 0.0,
                            "sale_amount_usd": 0.0,
                        },
                    ],
                }
            ],
        },
    )

    assert scenario_run["sampling_metadata"]["exogenous_model_id"] == "simple_exogenous_model"
    assert scenario_run["projection_run"]["scenario_set_id"] == "backend_smoke"
    assert scenario_run["projection_run"]["path_set_id"].startswith("path_set:")
    assert len(scenario_run["projection_run"]["scenario_input_ids"]) == 1
    assert [path["rollout_index"] for path in scenario_run["exogenous_paths"]] == [0, 1, 2]
    assert {path["path_set_id"] for path in scenario_run["exogenous_paths"]} == {
        scenario_run["projection_run"]["path_set_id"]
    }
    assert {path["exogenous_model_id"] for path in scenario_run["exogenous_paths"]} == {"simple_exogenous_model"}
    assert len({path["exogenous_path_id"] for path in scenario_run["exogenous_paths"]}) == 3
    assert all(0 <= path["seed"] <= 2**32 - 1 for path in scenario_run["exogenous_paths"])
    [result] = scenario_run["scenario_results"]
    assert result["scenario_id"] == "sp500_spend"
    assert len(result["projection_trajectories"]) == 3
    assert {trajectory["scenario_id"] for trajectory in result["projection_trajectories"]} == {"sp500_spend"}
    assert {trajectory["path_set_id"] for trajectory in result["projection_trajectories"]} == {
        scenario_run["projection_run"]["path_set_id"]
    }
    assert {trajectory["scenario_input_id"] for trajectory in result["projection_trajectories"]} == {
        scenario_run["projection_run"]["scenario_input_ids"][0]
    }
    assert result["monthly_columns"]["row_count"] == 12
    assert result["terminal_columns"]["row_count"] == 3
    assert result["metric_fan_columns"]["net_worth_usd"]["row_count"] == 4
    assert "p05" in result["metric_fan_columns"]["net_worth_usd"]["columns"]
    assert "p95" in result["metric_fan_columns"]["net_worth_usd"]["columns"]
    assert _sum(result["monthly_columns"]["columns"]["generic_sp500_sale_usd"]) > 0
    assert [status["status"] for status in result["rollout_statuses"]] == ["active", "active", "active"]


def test_backend_server_accepts_catalog_defaulted_property_selection(server_url: str) -> None:
    scenario_run = _post_json(
        server_url,
        "/api/scenario_sets/run",
        {
            "scenario_set_id": "backend_property_smoke",
            "title": "Backend property smoke",
            "sampling_request": {"exogenous_model_id": "simple", "rollout_count": 3, "horizon_months": 3, "seed": 11},
            "scenarios": [
                {
                    "scenario_id": "buy_catalog_property",
                    "label": "Buy catalog property",
                    "actors": [{"actor_id": "agent_a", "label": "Agent A", "role": "primary_owner"}],
                    "property_selection": {"property_id": "location_a_property"},
                    "financing": {"financing_mode": "fixed_30", "down_payment_pct": 20, "mortgage_rate_pct": 6.5},
                    "initial_balance_sheet": {
                        "accounts": [
                            {
                                "account_id": "checking",
                                "account_type": "checking",
                                "owner_actor_id": "agent_a",
                                "balance_usd": 300_000.0,
                            }
                        ],
                        "assets": [],
                    },
                }
            ],
        },
    )

    [result] = scenario_run["scenario_results"]
    assert result["summary"] == {"enabled": True, "property_id": "location_a_property", "location_id": "location_a"}
    columns = result["monthly_columns"]["columns"]
    assert _max(columns["property_value_usd"]) == 900_000.0
    assert _max(columns["mortgage_balance_usd"]) == 720_000.0
    assert _sum(columns["purchase_closing_cost_usd"]) == 67_500.0
    assert _sum(columns["mortgage_payment_usd"]) > 0
    assert _sum(columns["mortgage_interest_usd"]) > 0
    assert _sum(columns["mortgage_principal_usd"]) > 0
    assert _max(columns["home_equity_usd"]) > 180_000.0
    assert result["terminal_columns"]["row_count"] == 3


def test_backend_server_runs_browser_shaped_property_request(server_url: str) -> None:
    """The shared server should run a realistic browser payload through the runtime.

    This intentionally uses broad ranges. The test is guarding integration shape
    and obviously-wrong all-zero columns, not freezing exact stochastic paths.
    """

    scenario_run = _post_json(
        server_url,
        "/api/scenario_sets/run",
        {
            "scenario_set_id": "server_smoke",
            "title": "Server smoke",
            "sampling_request": {"rollout_count": 4, "horizon_months": 12, "seed": 11},
            "report_spec": {"percentiles": [5, 25, 50, 75, 95], "include_monthly_columns": True},
            "scenarios": [
                {
                    "scenario_id": "location_a_purchase",
                    "label": "Location A purchase",
                    "actors": [{"actor_id": "agent_a", "label": "Agent A", "role": "primary_owner"}],
                    "events": [
                        {
                            "event_id": "purchase",
                            "event_type": "property_purchase",
                            "month_index": 0,
                            "actor_id": "agent_a",
                            "property_id": "location_a_property",
                            "amount_usd": 900_000,
                            "description": "Property purchase at scenario start.",
                            "hoa_monthly_usd": 0,
                        },
                        {
                            "event_id": "mortgage",
                            "event_type": "mortgage_origination",
                            "month_index": 0,
                            "actor_id": "agent_a",
                            "property_id": "location_a_property",
                            "amount_usd": 675_000,
                            "description": "Mortgage originated at scenario start.",
                        },
                    ],
                    "property_selection": {"property_id": "location_a_property"},
                    "financing": {"financing_mode": "fixed_30", "down_payment_pct": 25, "mortgage_rate_pct": 6.5},
                    "occupancy_plan": {
                        "occupancy_mode": "owner_lives_in_property",
                        "owner_residence_property_id": "location_a_property",
                        "start_month": 0,
                        "end_month": 60,
                    },
                    "rental_plan": {"rental_mode": "not_rented"},
                    "transaction_costs": {"closing_cost_buy_pct": 2.5, "closing_cost_sell_pct": 6.5},
                    "property_assumptions": {
                        "insurance_annual_usd": 1_800,
                        "maintenance_pct": 1,
                        "depreciable_basis_pct": 80,
                    },
                    "initial_balance_sheet": {
                        "accounts": [
                            {
                                "account_id": "checking",
                                "account_type": "checking",
                                "owner_actor_id": "agent_a",
                                "balance_usd": 350_000,
                            }
                        ],
                        "assets": [
                            {
                                "asset_id": "private_holding_a",
                                "asset_type": "private_equity",
                                "owner_actor_id": "agent_a",
                                "units": 1_000,
                                "cost_basis_usd": 5_000,
                                "issuer_id": "private_holding_a",
                            }
                        ],
                        "liabilities": [],
                    },
                    "policies": [],
                }
            ],
        },
    )

    assert scenario_run["sampling_metadata"]["exogenous_model_id"] == "simple_exogenous_model"
    [result] = scenario_run["scenario_results"]
    assert result["scenario_id"] == "location_a_purchase"
    assert result["summary"] == {"enabled": True, "property_id": "location_a_property", "location_id": "location_a"}
    assert {status["status"] for status in result["rollout_statuses"]} == {"active"}
    assert result["metric_fan_columns"]["net_worth_usd"]["row_count"] == 13

    columns = result["monthly_columns"]["columns"]
    assert result["monthly_columns"]["row_count"] == 52
    assert 899_000 <= _max(columns["property_value_usd"]) <= 901_000
    assert 670_000 <= _max(columns["mortgage_balance_usd"]) <= 676_000
    assert 89_000 <= _sum(columns["purchase_closing_cost_usd"]) <= 91_000
    assert 180_000 <= _sum(columns["mortgage_payment_usd"]) <= 195_000
    assert 150_000 <= _sum(columns["mortgage_interest_usd"]) <= 185_000
    assert 20_000 <= _sum(columns["mortgage_principal_usd"]) <= 55_000
    assert 10_000 <= _min(columns["private_equity_value_usd"]) <= 30_000
    assert 20_000 <= _max(columns["private_equity_value_usd"]) <= 45_000
    assert 1_100_000 <= _max(columns["net_worth_usd"]) <= 1_350_000


def test_backend_server_runs_product_cash_spend_projection_metric_fan_and_rollout_detail(server_url: str) -> None:
    scenario = {
        "exogenous_model_id": "current_exogenous_model",
        "horizon_months": 3,
        "monthly_spend_usd": 1000.0,
        "spend_index": "none",
    }
    fan = _post_json(
        server_url,
        "/api/product/projections/metric_fan",
        {"scenario": scenario, "rollout_seeds": [7, 8], "metric": "cash_usd", "percentiles": [0, 50, 100]},
    )

    assert fan["exogenous_model_id"] == "simple_exogenous_model"
    assert "horizon_months" not in fan
    assert fan["metric"] == "cash_usd"
    assert fan["failed_count"] == 0
    assert "rollouts" not in fan
    assert [
        (summary["seed"], summary["failed"], summary["sort_rank"], summary["rank_percentile"])
        for summary in fan["rollout_summaries"]
    ] == [(7, False, 0, 25.0), (8, False, 1, 75.0)]
    assert [summary["terminal_metrics"]["cash_usd"] for summary in fan["rollout_summaries"]] == [247_000.0, 247_000.0]
    assert all("monthly_metrics" not in summary for summary in fan["rollout_summaries"])
    assert fan["monthly_metric_fan"] == {
        "row_count": 12,
        "columns": {
            "month_index": [0, 0, 0, 1, 1, 1, 2, 2, 2, 3, 3, 3],
            "percentile": [0.0, 50.0, 100.0] * 4,
            "value": [
                250_000.0,
                250_000.0,
                250_000.0,
                249_000.0,
                249_000.0,
                249_000.0,
                248_000.0,
                248_000.0,
                248_000.0,
                247_000.0,
                247_000.0,
                247_000.0,
            ],
        },
    }
    assert fan["terminal_metric_percentiles"] == {
        "row_count": 3,
        "columns": {"percentile": [0.0, 50.0, 100.0], "value": [247_000.0, 247_000.0, 247_000.0]},
    }

    public_security_fan = _post_json(
        server_url,
        "/api/product/projections/metric_fan",
        {"scenario": scenario, "rollout_seeds": [7, 8], "metric": "public_security_value_usd", "percentiles": [50]},
    )

    assert public_security_fan["metric"] == "public_security_value_usd"
    assert public_security_fan["monthly_metric_fan"]["row_count"] == 4
    assert public_security_fan["monthly_metric_fan"]["columns"]["month_index"] == [0, 1, 2, 3]
    assert public_security_fan["monthly_metric_fan"]["columns"]["percentile"] == [50.0] * 4
    assert public_security_fan["monthly_metric_fan"]["columns"]["value"][0] == 750_000.0

    detail = _post_json(server_url, "/api/product/projections/rollout", {"scenario": scenario, "seed": 7})

    assert detail["exogenous_model_id"] == "simple_exogenous_model"
    assert "horizon_months" not in detail
    assert detail["rollout"]["seed"] == 7
    assert detail["rollout"]["failed"] is False
    assert detail["rollout"]["monthly_metrics"]["row_count"] == 4
    columns = detail["rollout"]["monthly_metrics"]["columns"]
    assert columns["month_index"] == [0, 1, 2, 3]
    assert columns["cash_usd"] == [250_000.0, 249_000.0, 248_000.0, 247_000.0]
    assert columns["public_security_value_usd"][0] == 750_000.0
    assert columns["liquid_net_worth_usd"][0] == 1_000_000.0
    assert columns["net_worth_usd"][0] == 1_000_000.0
    assert set(columns) == {
        "month_index",
        "cash_usd",
        "public_security_value_usd",
        "liquid_net_worth_usd",
        "net_worth_usd",
        "shortfall_usd",
    }
    terminal = detail["rollout"]["terminal_metrics"]
    assert terminal["cash_usd"] == 247_000.0
    assert terminal["public_security_value_usd"] > 0
    assert terminal["liquid_net_worth_usd"] == pytest.approx(
        terminal["cash_usd"] + terminal["public_security_value_usd"]
    )
    assert terminal["net_worth_usd"] == pytest.approx(terminal["liquid_net_worth_usd"])
    assert set(terminal) == {
        "cash_usd",
        "public_security_value_usd",
        "liquid_net_worth_usd",
        "net_worth_usd",
        "shortfall_usd",
    }
    assert terminal["shortfall_usd"] == 0.0
    assert [event["kind"] for event in detail["rollout"]["events"]] == ["monthly_expense"] * 3
    assert [event["amount_paid_usd"] for event in detail["rollout"]["events"]] == [1_000.0, 1_000.0, 1_000.0]


def test_backend_server_product_portfolio_returns_configured_public_securities(server_url: str) -> None:
    portfolio = _get_json(server_url, "/api/product/portfolio")

    assert portfolio["as_of_date"] == "2026-05-14"
    assert portfolio["cash_usd"] == 250_000.0
    assert portfolio["total_public_security_value_usd"] == 750_000.0
    assert portfolio["total_public_security_cost_basis_usd"] == 550_000.0
    [position] = portfolio["public_securities"]
    assert position["account_label"] == "Taxable Brokerage"
    assert position["label"] == "SP500 Proxy"
    assert position["symbol"] == "VOO"
    assert position["security_kind"] == "etf"
    assert position["value_series_id"] == "sp500"
    assert position["unit_value_usd"] == 500.0
    assert position["quantity"] == 1_500.0
    assert position["current_value_usd"] == 750_000.0
    assert position["total_cost_basis_usd"] == 550_000.0
    assert [lot["lot_id"] for lot in position["lots"]] == ["sp500_proxy_2020_01", "sp500_proxy_2024_06"]
    assert [lot["holding_period_months_at_start"] for lot in position["lots"]] == [76, 23]
    assert [lot["quantity"] for lot in position["lots"]] == [750.0, 750.0]
    assert [lot["cost_basis_usd"] for lot in position["lots"]] == [300_000.0, 250_000.0]
    assert [lot["cost_basis_per_unit_usd"] for lot in position["lots"]] == [400.0, 333.3333333333333]


def test_backend_server_zeroes_failed_product_rollout_metrics(server_url: str) -> None:
    scenario = {
        "exogenous_model_id": "current_exogenous_model",
        "horizon_months": 3,
        "monthly_spend_usd": 300_000.0,
        "spend_index": "none",
        "funding_policy": {"cash_buffer_trigger_below_usd": 0.0, "cash_buffer_sale_usd": 0.0, "sell_order": []},
    }
    fan = _post_json(
        server_url,
        "/api/product/projections/metric_fan",
        {"scenario": scenario, "rollout_seeds": [7], "metric": "net_worth_usd", "percentiles": [50]},
    )

    assert fan["failed_count"] == 1
    assert fan["monthly_metric_fan"]["columns"]["month_index"] == [0, 1, 2, 3]
    assert fan["monthly_metric_fan"]["columns"]["value"] == [1_000_000.0, 0.0, 0.0, 0.0]
    [summary] = fan["rollout_summaries"]
    assert summary["failed"] is True
    assert summary["terminal_metrics"]["failed_month_index"] == 0
    assert summary["terminal_metrics"]["cash_usd"] == 0.0
    assert summary["terminal_metrics"]["public_security_value_usd"] == 0.0
    assert summary["terminal_metrics"]["net_worth_usd"] == 0.0
    assert summary["terminal_metrics"]["shortfall_usd"] == 300_000.0

    detail = _post_json(server_url, "/api/product/projections/rollout", {"scenario": scenario, "seed": 7})

    assert detail["rollout"]["failed"] is True
    assert detail["rollout"]["terminal_metrics"]["net_worth_usd"] == 0.0
    columns = detail["rollout"]["monthly_metrics"]["columns"]
    assert columns["month_index"] == [0, 1, 2, 3]
    assert columns["cash_usd"] == [250_000.0, 0.0, 0.0, 0.0]
    assert columns["public_security_value_usd"] == [750_000.0, 0.0, 0.0, 0.0]
    assert columns["net_worth_usd"] == [1_000_000.0, 0.0, 0.0, 0.0]
    expense, failure = detail["rollout"]["events"]
    assert expense == {
        "month_index": 0,
        "label": "Monthly expenses shortfall",
        "amount_usd": 0.0,
        "detail": "Required monthly spend",
        "kind": "monthly_expense",
        "amount_due_usd": 300_000.0,
        "amount_paid_usd": 0.0,
        "shortfall_usd": 300_000.0,
    }
    assert failure == {
        "month_index": 0,
        "label": "Rollout failed",
        "amount_usd": 300_000.0,
        "detail": "Required obligation could not be paid in full",
        "kind": "failure",
        "amount_due_usd": 300_000.0,
        "amount_paid_usd": 0.0,
        "shortfall_usd": 300_000.0,
    }


def test_backend_server_product_default_funding_sells_public_security_for_required_spend(server_url: str) -> None:
    scenario = {
        "exogenous_model_id": "current_exogenous_model",
        "horizon_months": 1,
        "monthly_spend_usd": 300_000.0,
        "spend_index": "none",
    }

    detail = _post_json(server_url, "/api/product/projections/rollout", {"scenario": scenario, "seed": 7})

    assert detail["rollout"]["failed"] is False
    columns = detail["rollout"]["monthly_metrics"]["columns"]
    assert columns["cash_usd"] == [250_000.0, 0.0]
    assert columns["public_security_value_usd"][0] == 750_000.0
    assert 0.0 < columns["public_security_value_usd"][1] < 750_000.0
    terminal = detail["rollout"]["terminal_metrics"]
    assert terminal["cash_usd"] == 0.0
    assert terminal["shortfall_usd"] == 0.0
    assert terminal["net_worth_usd"] == pytest.approx(columns["public_security_value_usd"][1])
    sale, expense = detail["rollout"]["events"]
    assert sale == {
        "month_index": 0,
        "label": "Sold SP500 Proxy (VOO)",
        "amount_usd": 50_000.0,
        "detail": "Public-security sale",
        "kind": "public_security_sale",
        "asset_id": "sp500",
        "asset_label": "SP500 Proxy (VOO)",
        "units": 100.0,
        "proceeds_usd": 50_000.0,
        "cost_basis_usd": 40_000.0,
    }
    assert expense == {
        "month_index": 0,
        "label": "Paid monthly expenses",
        "amount_usd": 300_000.0,
        "detail": "Required monthly spend",
        "kind": "monthly_expense",
        "amount_due_usd": 300_000.0,
        "amount_paid_usd": 300_000.0,
        "shortfall_usd": 0.0,
    }


def test_backend_server_product_cash_buffer_uses_trigger_and_fixed_sale_amount(server_url: str) -> None:
    scenario = {
        "exogenous_model_id": "current_exogenous_model",
        "horizon_months": 1,
        "monthly_spend_usd": 1_000.0,
        "spend_index": "none",
        "funding_policy": {
            "cash_buffer_trigger_below_usd": 260_000.0,
            "cash_buffer_sale_usd": 20_000.0,
            "sell_order": ["public_securities"],
        },
    }

    detail = _post_json(server_url, "/api/product/projections/rollout", {"scenario": scenario, "seed": 7})

    assert detail["rollout"]["failed"] is False
    columns = detail["rollout"]["monthly_metrics"]["columns"]
    assert columns["cash_usd"] == [250_000.0, 269_000.0]
    assert detail["rollout"]["terminal_metrics"]["cash_usd"] == 269_000.0
    assert detail["rollout"]["terminal_metrics"]["shortfall_usd"] == 0.0
    sale, expense = detail["rollout"]["events"]
    assert sale["kind"] == "public_security_sale"
    assert sale["proceeds_usd"] == pytest.approx(20_000.0)
    assert expense["kind"] == "monthly_expense"
    assert expense["amount_paid_usd"] == 1_000.0


def test_backend_server_product_rollout_includes_zero_tax_accrual_events_without_taxable_income(
    server_url: str,
) -> None:
    scenario = {
        "exogenous_model_id": "current_exogenous_model",
        "horizon_months": 12,
        "monthly_spend_usd": 1_000.0,
        "spend_index": "none",
        "funding_policy": {"cash_buffer_trigger_below_usd": 0.0, "cash_buffer_sale_usd": 0.0, "sell_order": []},
    }

    detail = _post_json(server_url, "/api/product/projections/rollout", {"scenario": scenario, "seed": 7})

    tax_accruals = [event for event in detail["rollout"]["events"] if event["kind"] == "tax_accrual"]
    assert {event["jurisdiction_id"] for event in tax_accruals} == {"federal_us", "california"}
    assert {event["month_index"] for event in tax_accruals} == {11}
    assert all(event["amount_usd"] == 0.0 for event in tax_accruals)
    assert [event for event in detail["rollout"]["events"] if event["kind"] == "tax_payment"] == []


def test_backend_server_product_rollout_includes_federal_and_california_tax_events_for_public_security_sales(
    server_url: str,
) -> None:
    scenario = {
        "exogenous_model_id": "current_exogenous_model",
        "horizon_months": 13,
        "monthly_spend_usd": 1_000.0,
        "spend_index": "none",
        "funding_policy": {
            "cash_buffer_trigger_below_usd": 260_000.0,
            "cash_buffer_sale_usd": 500_000.0,
            "sell_order": ["public_securities"],
        },
    }

    detail = _post_json(server_url, "/api/product/projections/rollout", {"scenario": scenario, "seed": 7})

    events = detail["rollout"]["events"]
    tax_accruals = [event for event in events if event["kind"] == "tax_accrual"]
    assert {event["jurisdiction_id"] for event in tax_accruals} == {"federal_us", "california"}
    assert {event["month_index"] for event in tax_accruals} == {11}
    assert all(event["amount_usd"] > 0 for event in tax_accruals)
    assert sum(event["amount_usd"] for event in tax_accruals) == pytest.approx(
        sum(event["total_tax_usd"] for event in tax_accruals)
    )
    federal = next(event for event in tax_accruals if event["jurisdiction_id"] == "federal_us")
    california = next(event for event in tax_accruals if event["jurisdiction_id"] == "california")
    assert federal["capital_gain_tax_usd"] > 0
    assert california["capital_gain_tax_usd"] == 0.0
    assert california["ordinary_tax_usd"] > 0

    tax_payments = [event for event in events if event["kind"] == "tax_payment"]
    [tax_payment] = tax_payments
    assert tax_payment["month_index"] == 12
    assert tax_payment["obligation_type"] == "tax_true_up"
    assert tax_payment["amount_due_usd"] == pytest.approx(sum(event["amount_usd"] for event in tax_accruals))
    assert tax_payment["amount_paid_usd"] == pytest.approx(tax_payment["amount_due_usd"])
    assert tax_payment["shortfall_usd"] == 0.0


if __name__ == "__main__":
    pytest_bazel.main()
