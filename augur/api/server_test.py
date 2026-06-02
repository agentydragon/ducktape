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
from fastapi.testclient import TestClient

from util.bazel.runfiles import get_required_path
from util.net import pick_free_port
from util.testing.undeclared_outputs import undeclared_outputs_dir


@pytest.fixture(scope="module")
def server_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    tmp_path = tmp_path_factory.mktemp("augur_server")
    out = undeclared_outputs_dir()
    server_log = (out / "augur-server.log").open("w")
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
            "AUGUR_API_IMAGE_TAG": "devel-20260527220657-b86700d",
            "HOME": str(tmp_path / "home"),
            "AUGUR_FRONTEND_IMAGE_TAG": "devel-20260527194755-6ec68c0",
            "MPLCONFIGDIR": str(tmp_path / "matplotlib"),
            "PYTHONUNBUFFERED": "1",
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
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


def test_backend_server_runs_product_cash_spend_projection_metric_fan_and_rollout_detail(server_url: str) -> None:
    scenario = {"model_id": "current_model", "horizon_months": 3, "monthly_spend_usd": 1000.0, "spend_index": "none"}
    fan = _post_json(
        server_url,
        "/api/product/projections/metric_fan",
        {"scenario": scenario, "rollout_seeds": [7, 8], "metric": "cash_usd", "percentiles": [0, 50, 100]},
    )

    assert fan["model_id"] == "composite_exogenous_model"
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
    }
    assert fan["terminal_metric_percentiles"] == {
        "percentile": [0.0, 50.0, 100.0],
        "value": [247_000.0, 247_000.0, 247_000.0],
    }

    holding_fan = _post_json(
        server_url,
        "/api/product/projections/metric_fan",
        {"scenario": scenario, "rollout_seeds": [7, 8], "metric": "holding_value_usd", "percentiles": [50]},
    )

    assert holding_fan["metric"] == "holding_value_usd"
    assert holding_fan["monthly_metric_fan"]["month_index"] == [0, 1, 2, 3]
    assert holding_fan["monthly_metric_fan"]["percentile"] == [50.0] * 4
    assert holding_fan["monthly_metric_fan"]["value"][0] == 835_500.0

    detail = _post_json(server_url, "/api/product/projections/rollout", {"scenario": scenario, "seed": 7})

    assert detail["model_id"] == "composite_exogenous_model"
    assert "horizon_months" not in detail
    assert detail["rollout"]["seed"] == 7
    assert detail["rollout"]["failed"] is False
    columns = detail["rollout"]["monthly_metrics"]
    assert len(columns["month_index"]) == 4
    assert columns["month_index"] == [0, 1, 2, 3]
    assert columns["cash_usd"] == [250_000.0, 249_000.0, 248_000.0, 247_000.0]
    assert columns["holding_value_usd"][0] == 835_500.0
    assert columns["liquid_net_worth_usd"][0] == 1_085_500.0
    # +$25k for the PHA private-equity position (1000 units at $25 anchor).
    assert columns["net_worth_usd"][0] == 1_110_500.0
    assert set(columns) == {
        "month_index",
        "cash_usd",
        "holding_value_usd",
        "private_equity_value_usd",
        "property_value_usd",
        "mortgage_balance_usd",
        "home_equity_usd",
        "liquid_net_worth_usd",
        "net_worth_usd",
        "shortfall_usd",
    }
    terminal = detail["rollout"]["terminal_metrics"]
    assert terminal["cash_usd"] == 247_000.0
    assert terminal["holding_value_usd"] > 0
    assert terminal["private_equity_value_usd"] > 0
    assert terminal["liquid_net_worth_usd"] == pytest.approx(terminal["cash_usd"] + terminal["holding_value_usd"])
    assert terminal["net_worth_usd"] == pytest.approx(
        terminal["liquid_net_worth_usd"] + terminal["private_equity_value_usd"]
    )
    assert set(terminal) == {
        "cash_usd",
        "holding_value_usd",
        "private_equity_value_usd",
        "property_value_usd",
        "mortgage_balance_usd",
        "home_equity_usd",
        "liquid_net_worth_usd",
        "net_worth_usd",
        "shortfall_usd",
    }
    assert terminal["shortfall_usd"] == 0.0
    assert [event["kind"] for event in detail["rollout"]["events"]] == ["monthly_expense"] * 3
    assert [event["amount_paid_usd"] for event in detail["rollout"]["events"]] == [1_000.0, 1_000.0, 1_000.0]


def test_backend_server_product_portfolio_returns_configured_holdings(server_url: str) -> None:
    portfolio = _get_json(server_url, "/api/product/portfolio")

    assert portfolio["as_of_date"] == "2026-05-14"
    assert portfolio["cash_usd"] == 250_000.0
    # SP500: 1500 * $500 = $750k; BTC: 1 * $75k = $75k; ETH: 5 * $2.1k = $10.5k;
    # PHA (PE): 1000 * $25 = $25k.
    assert portfolio["total_holdings_value_usd"] == 860_500.0
    # SP500 basis $550k + BTC basis $30k + ETH basis $15k + PHA basis $5k = $600k.
    assert portfolio["total_holdings_cost_basis_usd"] == 600_000.0
    positions_by_id = {position["position_id"]: position for position in portfolio["holdings"]}
    assert set(positions_by_id) == {"sp500_proxy", "btc_holding", "eth_holding", "private_holding_a"}
    sp500 = positions_by_id["sp500_proxy"]
    assert sp500["account_label"] == "Taxable Brokerage"
    assert sp500["label"] == "SP500 Proxy"
    assert sp500["symbol"] == "VOO"
    assert sp500["security_kind"] == "etf"
    assert sp500["value_series_id"] == "sp500"
    assert sp500["unit_value_usd"] == 500.0
    assert sp500["quantity"] == 1_500.0
    assert sp500["current_value_usd"] == 750_000.0
    assert sp500["total_cost_basis_usd"] == 550_000.0
    assert [lot["lot_id"] for lot in sp500["lots"]] == ["sp500_proxy_2020_01", "sp500_proxy_2024_06"]
    assert [lot["holding_period_months_at_start"] for lot in sp500["lots"]] == [76, 23]
    assert [lot["quantity"] for lot in sp500["lots"]] == [750.0, 750.0]
    assert [lot["cost_basis_usd"] for lot in sp500["lots"]] == [300_000.0, 250_000.0]
    assert [lot["cost_basis_per_unit_usd"] for lot in sp500["lots"]] == [400.0, 333.3333333333333]
    btc = positions_by_id["btc_holding"]
    assert btc["symbol"] == "BTC"
    assert btc["security_kind"] == "cryptocurrency"
    assert btc["value_series_id"] == "crypto:btc"
    assert btc["unit_value_usd"] == 75_000.0
    assert btc["current_value_usd"] == 75_000.0
    eth = positions_by_id["eth_holding"]
    assert eth["symbol"] == "ETH"
    assert eth["security_kind"] == "cryptocurrency"
    assert eth["value_series_id"] == "crypto:eth"
    assert eth["unit_value_usd"] == 2_100.0
    assert eth["current_value_usd"] == 10_500.0
    pha = positions_by_id["private_holding_a"]
    assert pha["symbol"] == "PHA"
    assert pha["security_kind"] == "private_equity"
    assert pha["value_series_id"] == "private_equity:private_holding_a"
    assert pha["unit_value_usd"] == 25.0
    assert pha["current_value_usd"] == 25_000.0
    assert pha["total_cost_basis_usd"] == 5_000.0


def test_backend_server_exposes_deployment_image_commits(server_url: str) -> None:
    deployment = _get_json(server_url, "/api/deployment")

    assert deployment == {
        "api": {
            "image_tag": "devel-20260527220657-b86700d",
            "source_commit": "b86700d",
            "source_commit_url": "https://github.com/agentydragon/ducktape/commit/b86700d",
        },
        "frontend": {
            "image_tag": "devel-20260527194755-6ec68c0",
            "source_commit": "6ec68c0",
            "source_commit_url": "https://github.com/agentydragon/ducktape/commit/6ec68c0",
        },
    }


def test_backend_server_zeroes_failed_product_rollout_metrics(server_url: str) -> None:
    scenario = {
        "model_id": "current_model",
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
    assert fan["monthly_metric_fan"]["month_index"] == [0, 1, 2, 3]
    # Month 0 = cash 250k + holdings 835.5k + PHA 25k; failure zeros subsequent months.
    assert fan["monthly_metric_fan"]["value"] == [1_110_500.0, 0.0, 0.0, 0.0]
    [summary] = fan["rollout_summaries"]
    assert summary["failed"] is True
    assert summary["terminal_metrics"]["failed_month_index"] == 0
    assert summary["terminal_metrics"]["cash_usd"] == 0.0
    assert summary["terminal_metrics"]["holding_value_usd"] == 0.0
    assert summary["terminal_metrics"]["net_worth_usd"] == 0.0
    assert summary["terminal_metrics"]["shortfall_usd"] == 300_000.0

    detail = _post_json(server_url, "/api/product/projections/rollout", {"scenario": scenario, "seed": 7})

    assert detail["rollout"]["failed"] is True
    assert detail["rollout"]["terminal_metrics"]["net_worth_usd"] == 0.0
    columns = detail["rollout"]["monthly_metrics"]
    assert columns["month_index"] == [0, 1, 2, 3]
    assert columns["cash_usd"] == [250_000.0, 0.0, 0.0, 0.0]
    assert columns["holding_value_usd"] == [835_500.0, 0.0, 0.0, 0.0]
    assert columns["net_worth_usd"] == [1_110_500.0, 0.0, 0.0, 0.0]
    expense, failure = detail["rollout"]["events"]
    assert expense == {
        "month_index": 0,
        "amount_usd": 0.0,
        "kind": "monthly_expense",
        "amount_due_usd": 300_000.0,
        "amount_paid_usd": 0.0,
        "shortfall_usd": 300_000.0,
    }
    assert failure == {
        "month_index": 0,
        "amount_usd": 300_000.0,
        "kind": "failure",
        "amount_due_usd": 300_000.0,
        "amount_paid_usd": 0.0,
        "shortfall_usd": 300_000.0,
    }


def test_backend_server_product_default_funding_sells_holding_for_required_spend(server_url: str) -> None:
    scenario = {"model_id": "current_model", "horizon_months": 1, "monthly_spend_usd": 300_000.0, "spend_index": "none"}

    detail = _post_json(server_url, "/api/product/projections/rollout", {"scenario": scenario, "seed": 7})

    assert detail["rollout"]["failed"] is False
    columns = detail["rollout"]["monthly_metrics"]
    assert columns["cash_usd"] == [250_000.0, 0.0]
    assert columns["holding_value_usd"][0] == 835_500.0
    assert 0.0 < columns["holding_value_usd"][1] < 835_500.0
    terminal = detail["rollout"]["terminal_metrics"]
    assert terminal["cash_usd"] == 0.0
    assert terminal["shortfall_usd"] == 0.0
    assert terminal["net_worth_usd"] == pytest.approx(
        columns["holding_value_usd"][1] + columns["private_equity_value_usd"][1]
    )
    sale, expense = detail["rollout"]["events"]
    assert sale == {
        "month_index": 0,
        "amount_usd": 50_000.0,
        "kind": "holding_sale",
        "asset": {"kind": "sp500"},
        "asset_label": "SP500 Proxy (VOO)",
        "units": 100.0,
        "proceeds_usd": 50_000.0,
        "cost_basis_usd": 40_000.0,
    }
    assert expense == {
        "month_index": 0,
        "amount_usd": 300_000.0,
        "kind": "monthly_expense",
        "amount_due_usd": 300_000.0,
        "amount_paid_usd": 300_000.0,
        "shortfall_usd": 0.0,
    }


def test_api_product_rollout_includes_private_equity_protocol_event_and_forced_sale(
    forced_private_equity_event_client: TestClient,
) -> None:
    response = forced_private_equity_event_client.post(
        "/api/product/projections/rollout",
        json={
            "scenario": {
                "model_id": "current_model",
                "horizon_months": 2,
                "monthly_spend_usd": 1_000.0,
                "spend_index": "none",
                "funding_policy": {"sell_order": []},
            },
            "seed": 7,
        },
    )

    assert response.status_code == 200
    detail = response.json()
    assert detail["model_id"] == "forced_pe_fixture"

    [pe_event] = [event for event in detail["rollout"]["events"] if event["kind"] == "private_equity_event"]
    assert pe_event["month_index"] == 1
    assert pe_event["asset"] == {"kind": "private_equity", "issuer_id": "private_holding_a"}
    assert pe_event["asset_label"] == "Private Holding A (PHA)"
    assert pe_event["event_kind"] == "acquisition_cashout"
    assert pe_event["regime"] == "acquired"
    assert pe_event["mark_usd"] == pytest.approx(25.0)
    assert pe_event["forced_sale_fraction"] == pytest.approx(0.25)

    [sale] = [
        event
        for event in detail["rollout"]["events"]
        if event["kind"] == "holding_sale"
        and event["asset"] == {"kind": "private_equity", "issuer_id": "private_holding_a"}
    ]
    assert sale["units"] == pytest.approx(250.0)
    assert sale["proceeds_usd"] == pytest.approx(6_250.0)


def test_api_product_metric_fan_respects_private_equity_tender_capacity(
    capacity_limited_private_equity_client: TestClient,
) -> None:
    scenario = {
        "model_id": "current_model",
        "horizon_months": 2,
        "monthly_spend_usd": 1_000.0,
        "spend_index": "none",
        "funding_policy": {"sell_order": []},
        "pe_tender_policy": {"liquid_net_worth_floor_usd": 1_200_000.0, "index_floor_to_inflation": False},
    }

    fan_response = capacity_limited_private_equity_client.post(
        "/api/product/projections/metric_fan",
        json={"scenario": scenario, "rollout_seeds": [7], "metric": "cash_usd", "percentiles": [50]},
    )

    assert fan_response.status_code == 200
    fan = fan_response.json()
    assert fan["model_id"] == "capacity_limited_pe_fixture"
    assert fan["terminal_metric_percentiles"] == {"percentile": [50.0], "value": [254_250.0]}
    [summary] = fan["rollout_summaries"]
    assert summary["terminal_metrics"]["cash_usd"] == pytest.approx(254_250.0)
    assert summary["terminal_metrics"]["private_equity_value_usd"] == pytest.approx(18_750.0)

    rollout_response = capacity_limited_private_equity_client.post(
        "/api/product/projections/rollout", json={"scenario": scenario, "seed": 7}
    )

    assert rollout_response.status_code == 200
    detail = rollout_response.json()
    assert detail["rollout"]["terminal_metrics"]["cash_usd"] == pytest.approx(254_250.0)
    assert detail["rollout"]["terminal_metrics"]["private_equity_value_usd"] == pytest.approx(18_750.0)
    [opportunity] = [event for event in detail["rollout"]["events"] if event["kind"] == "private_equity_opportunity"]
    assert opportunity["event_kind"] == "tender"
    assert opportunity["outcome"] == "sold"
    assert opportunity["sellable_units"] == pytest.approx(250.0)
    assert opportunity["target_units"] == pytest.approx(250.0)
    assert opportunity["proceeds_usd"] == pytest.approx(6_250.0)


def test_backend_server_product_cash_buffer_uses_trigger_and_fixed_sale_amount(server_url: str) -> None:
    scenario = {
        "model_id": "current_model",
        "horizon_months": 1,
        "monthly_spend_usd": 1_000.0,
        "spend_index": "none",
        "funding_policy": {
            "cash_buffer_trigger_below_usd": 260_000.0,
            "cash_buffer_sale_usd": 20_000.0,
            "sell_order": ["stocks"],
        },
    }

    detail = _post_json(server_url, "/api/product/projections/rollout", {"scenario": scenario, "seed": 7})

    assert detail["rollout"]["failed"] is False
    columns = detail["rollout"]["monthly_metrics"]
    assert columns["cash_usd"] == [250_000.0, 269_000.0]
    assert detail["rollout"]["terminal_metrics"]["cash_usd"] == 269_000.0
    assert detail["rollout"]["terminal_metrics"]["shortfall_usd"] == 0.0
    sale, expense = detail["rollout"]["events"]
    assert sale["kind"] == "holding_sale"
    assert sale["proceeds_usd"] == pytest.approx(20_000.0)
    assert expense["kind"] == "monthly_expense"
    assert expense["amount_paid_usd"] == 1_000.0


def test_backend_server_product_rollout_includes_zero_tax_accrual_events_without_taxable_income(
    server_url: str,
) -> None:
    scenario = {
        "model_id": "current_model",
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


def test_backend_server_product_rollout_includes_federal_and_california_tax_events_for_holding_sales(
    server_url: str,
) -> None:
    scenario = {
        "model_id": "current_model",
        "horizon_months": 13,
        "monthly_spend_usd": 1_000.0,
        "spend_index": "none",
        "funding_policy": {
            "cash_buffer_trigger_below_usd": 260_000.0,
            "cash_buffer_sale_usd": 500_000.0,
            "sell_order": ["stocks"],
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


def test_backend_server_product_outside_rent_re_pegs_yearly(server_url: str) -> None:
    scenario = {
        "model_id": "current_model",
        "horizon_months": 13,
        "monthly_spend_usd": 1000.0,
        "spend_index": "none",
        "monthly_rent_usd": 3000.0,
        "rental_location_id": "location_a",
        "funding_policy": {"cash_buffer_trigger_below_usd": 0.0, "cash_buffer_sale_usd": 0.0, "sell_order": []},
    }

    detail = _post_json(server_url, "/api/product/projections/rollout", {"scenario": scenario, "seed": 7})

    rent_events = [event for event in detail["rollout"]["events"] if event["kind"] == "outside_rent"]
    assert len(rent_events) == 13
    year_zero = [event for event in rent_events if event["month_index"] < 12]
    assert {event["amount_paid_usd"] for event in year_zero} == {3000.0}
    [year_one_event] = [event for event in rent_events if event["month_index"] == 12]
    assert year_one_event["amount_paid_usd"] != 3000.0


def test_backend_server_product_rent_rejects_unknown_location(server_url: str) -> None:
    scenario = {
        "model_id": "current_model",
        "horizon_months": 3,
        "monthly_spend_usd": 1000.0,
        "spend_index": "none",
        "monthly_rent_usd": 3000.0,
        "rental_location_id": "not_a_real_location",
        "funding_policy": {"cash_buffer_trigger_below_usd": 0.0, "cash_buffer_sale_usd": 0.0, "sell_order": []},
    }

    request = urllib.request.Request(
        f"{server_url}/api/product/projections/rollout",
        data=json.dumps({"scenario": scenario, "seed": 7}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(request, timeout=240)
    assert exc_info.value.code == 400


def test_backend_server_pe_tender_sale_appears_as_holding_sale_event(server_url: str) -> None:
    """PE tender sales should surface as `holding_sale` events in rollout detail."""
    scenario = {
        "model_id": "current_model",
        "horizon_months": 48,
        "monthly_spend_usd": 1_000.0,
        "spend_index": "none",
        "pe_tender_policy": {"liquid_net_worth_floor_usd": 5_000_000.0, "index_floor_to_inflation": False},
    }
    # Request several seeds to maximize chance of hitting a tender event (λ≈1/year over 48mo).
    detail = _post_json(server_url, "/api/product/projections/rollout", {"scenario": scenario, "seed": 7})
    pe_sales = [
        event
        for event in detail["rollout"]["events"]
        if event["kind"] == "holding_sale" and event["asset"]["kind"] == "private_equity"
    ]
    assert len(pe_sales) >= 1, (
        f"expected at least 1 PE holding_sale event, got {len(pe_sales)}; "
        f"all events: {[e['kind'] for e in detail['rollout']['events']]}"
    )
    sale = pe_sales[0]
    assert sale["proceeds_usd"] > 0
    assert sale["units"] > 0


if __name__ == "__main__":
    pytest_bazel.main()
