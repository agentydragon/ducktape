"""Smoke the generic Augur server backend."""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, cast

import pytest
import pytest_bazel
from fastapi.testclient import TestClient

from util.bazel.runfiles import get_required_path
from util.net import pick_free_port
from util.testing.undeclared_outputs import undeclared_outputs_dir

# TestClient drives the app over httpx, imported inside starlette; gazelle cannot see it.
# gazelle:include_dep @pypi//httpx


def _usd_quanta(value: float | int) -> str:
    return str(int((Decimal(str(value)) / Decimal("0.01")).to_integral_value(rounding=ROUND_HALF_UP)))


@pytest.fixture(scope="module")
def server_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    tmp_path = tmp_path_factory.mktemp("augur_server")
    out = undeclared_outputs_dir()
    server_log = (out / "augur-server.log").open("w")
    port = pick_free_port("127.0.0.1")
    server = subprocess.Popen(
        [
            str(get_required_path("_main/finance/augur/api/server_bin")),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--config",
            str(get_required_path("_main/finance/augur/api/testdata/config.yaml")),
            "--api-only",
        ],
        env={
            **os.environ,
            "AUGUR_API_IMAGE_TAG": "devel-20260527220657-b86700d",
            # An (empty) evidence checkout: the price readers boot against it and
            # would report any market as not-mirrored; these tests never read one.
            "AUGUR_EVIDENCE_DIR": str(tmp_path / "evidence"),
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


def test_backend_server_catalog_serializes_configured_money_as_decimal_strings(server_url: str) -> None:
    catalog = _get_json(server_url, "/api/catalog")

    property_ = cast(list[dict[str, Any]], catalog["properties"])[0]
    assert property_["price"] == "900000"
    assert property_["hoa_monthly"] == "0"


def test_backend_server_runs_product_cash_spend_projection_metric_fan_and_rollout_detail(server_url: str) -> None:
    scenario = {"model_id": "current_model", "horizon_months": 3, "monthly_spend": 1000, "spend_index": "none"}
    fan = _post_json(
        server_url,
        "/api/product/projections/metric_fan",
        {"scenario": scenario, "first_seed": 7, "rollout_count": 2, "metric": "cash", "percentiles": [0, 50, 100]},
    )

    assert fan["model_id"] == "composite"
    assert "horizon_months" not in fan
    assert fan["currency_code"] == "USD"
    assert fan["currency_quantum"] == "0.01"
    assert fan["metric"] == "cash"
    assert fan["failed_count"] == 0
    assert "rollouts" not in fan
    assert "rollout_summaries" not in fan
    assert fan["monthly_metric_fan"] == {
        "month_index": [0, 0, 0, 1, 1, 1, 2, 2, 2, 3, 3, 3],
        "percentile": [0.0, 50.0, 100.0] * 4,
        "value_quanta": [
            "25000000",
            "25000000",
            "25000000",
            # +$1,875 of coupon against -$1,000 of spend: both rungs pay at month 0, the TIPS
            # $1,000 (100k at 2%/yr, semiannual) and the muni $875 (50k at 3.5%/yr).
            "25087500",
            "25087500",
            "25087500",
            "24987500",
            "24987500",
            "24987500",
            "24887500",
            "24887500",
            "24887500",
        ],
    }
    assert fan["terminal_metric_percentiles"] == {
        "percentile": [0.0, 50.0, 100.0],
        "value_quanta": ["24887500", "24887500", "24887500"],
    }

    terminal_distribution = _post_json(
        server_url,
        "/api/product/projections/terminal_distribution",
        {
            "scenario": scenario,
            "first_seed": 7,
            "rollout_count": 2,
            "metric": "cash",
            "percentiles": [0, 1, 2, 50, 100],
        },
    )

    assert terminal_distribution["model_id"] == "composite"
    assert terminal_distribution["currency_code"] == "USD"
    assert terminal_distribution["currency_quantum"] == "0.01"
    assert terminal_distribution["metric"] == "cash"
    assert terminal_distribution["failed_count"] == 0
    assert "monthly_metric_fan" not in terminal_distribution
    assert "rollouts" not in terminal_distribution
    assert "rollout_summaries" not in terminal_distribution
    assert terminal_distribution["terminal_metric_percentiles"] == {
        "percentile": [0.0, 1.0, 2.0, 50.0, 100.0],
        "value_quanta": ["24887500", "24887500", "24887500", "24887500", "24887500"],
    }
    assert terminal_distribution["terminal_metric_samples"] == {
        "seed": [7, 8],
        "value_quanta": ["24887500", "24887500"],
        "failed": [False, False],
    }

    combined = _post_json(
        server_url,
        "/api/product/projections/summary",
        {
            "scenario": scenario,
            "first_seed": 7,
            "rollout_count": 2,
            "metric": "cash",
            "fan_percentiles": [0, 50, 100],
            "terminal_percentiles": [0, 1, 2, 50, 100],
        },
    )
    assert combined["metric_fan"] == fan
    assert combined["terminal_distribution"] == terminal_distribution

    holding_fan = _post_json(
        server_url,
        "/api/product/projections/metric_fan",
        {"scenario": scenario, "first_seed": 7, "rollout_count": 2, "metric": "holding_value", "percentiles": [50]},
    )

    assert holding_fan["metric"] == "holding_value"
    assert holding_fan["monthly_metric_fan"]["month_index"] == [0, 1, 2, 3]
    assert holding_fan["monthly_metric_fan"]["percentile"] == [50.0] * 4
    assert holding_fan["monthly_metric_fan"]["value_quanta"][0] == "83550000"

    detail = _post_json(server_url, "/api/product/projections/rollout", {"scenario": scenario, "seed": 7})

    assert detail["model_id"] == "composite"
    assert "horizon_months" not in detail
    assert detail["rollout"]["seed"] == 7
    assert detail["rollout"]["failed"] is False
    columns = detail["rollout"]["monthly_metrics"]
    assert len(columns["month_index"]) == 4
    assert columns["month_index"] == [0, 1, 2, 3]
    assert detail["currency_code"] == "USD"
    assert detail["currency_quantum"] == "0.01"
    assert columns["cash_quanta"] == ["25000000", "25087500", "24987500", "24887500"]
    assert columns["holding_value_quanta"][0] == "83550000"
    assert columns["liquid_net_worth_quanta"][0] == "108550000"
    # +$25k for the PHA private-equity position (1000 units at $25 anchor), +$150k for the two
    # bond rungs at face — in net worth, and deliberately not in liquid net worth above.
    assert columns["net_worth_quanta"][0] == "126050000"
    assert set(columns) == {
        "month_index",
        "cash_quanta",
        "holding_value_quanta",
        "private_equity_value_quanta",
        "bond_value_quanta",
        "property_value_quanta",
        "mortgage_balance_quanta",
        "home_equity_quanta",
        "liquid_net_worth_quanta",
        "net_worth_quanta",
        "shortfall_quanta",
    }
    terminal = detail["rollout"]["terminal_metrics"]
    assert terminal["cash_quanta"] == "24887500"
    assert int(terminal["holding_value_quanta"]) > 0
    assert int(terminal["private_equity_value_quanta"]) > 0
    assert int(terminal["liquid_net_worth_quanta"]) == int(terminal["cash_quanta"]) + int(
        terminal["holding_value_quanta"]
    )
    # Bonds are the third term: liquid net worth deliberately excludes them (held to maturity,
    # neither marked nor saleable) while net worth includes them at face.
    assert int(terminal["net_worth_quanta"]) == (
        int(terminal["liquid_net_worth_quanta"])
        + int(terminal["private_equity_value_quanta"])
        + int(terminal["bond_value_quanta"])
    )
    assert set(terminal) == {
        "cash_quanta",
        "holding_value_quanta",
        "private_equity_value_quanta",
        "bond_value_quanta",
        "property_value_quanta",
        "mortgage_balance_quanta",
        "home_equity_quanta",
        "liquid_net_worth_quanta",
        "net_worth_quanta",
        "shortfall_quanta",
    }
    assert terminal["shortfall_quanta"] == "0"

    assert [event["kind"] for event in detail["rollout"]["events"]] == ["monthly_expense"] * 3
    assert [event["amount_paid_quanta"] for event in detail["rollout"]["events"]] == ["100000", "100000", "100000"]


def test_backend_server_product_portfolio_returns_configured_holdings(server_url: str) -> None:
    portfolio = _get_json(server_url, "/api/product/portfolio")

    assert portfolio["as_of_date"] == "2026-05-14"
    assert portfolio["currency_code"] == "USD"
    assert portfolio["currency_quantum"] == "0.01"
    assert portfolio["cash_quanta"] == "25000000"
    # SP500: 1500 * $500 = $750k; BTC: 1 * $75k = $75k; ETH: 5 * $2.1k = $10.5k;
    # PHA (PE): 1000 * $25 = $25k.
    assert portfolio["total_holdings_value_quanta"] == "86050000"
    # SP500 basis $549,997.50 + BTC $30k + ETH $15k + PHA $5k = $599,997.50.
    assert portfolio["total_holdings_cost_basis_quanta"] == "59999750"
    positions_by_id = {position["position_id"]: position for position in portfolio["holdings"]}
    assert set(positions_by_id) == {"sp500_proxy", "btc_holding", "eth_holding", "private_holding_a"}
    sp500 = positions_by_id["sp500_proxy"]
    assert sp500["account_label"] == "Taxable Brokerage"
    assert sp500["label"] == "SP500 Proxy"
    assert sp500["symbol"] == "VOO"
    assert sp500["security_kind"] == "etf"
    assert sp500["asset"] == {"kind": "security", "symbol": "VOO"}
    assert sp500["unit_value_quanta"] == "50000"
    assert sp500["quantity"] == 1_500.0
    assert sp500["current_value_quanta"] == "75000000"
    assert sp500["total_cost_basis_quanta"] == "54999750"
    assert [lot["lot_id"] for lot in sp500["lots"]] == ["sp500_proxy_2020_01", "sp500_proxy_2024_06"]
    assert [lot["holding_period_months_at_start"] for lot in sp500["lots"]] == [76, 23]
    assert [lot["quantity"] for lot in sp500["lots"]] == [750.0, 750.0]
    assert [lot["cost_basis_quanta"] for lot in sp500["lots"]] == ["30000000", "24999750"]
    assert [lot["cost_basis_per_unit_quanta"] for lot in sp500["lots"]] == ["40000", "33333"]
    btc = positions_by_id["btc_holding"]
    assert btc["symbol"] == "btc"
    assert btc["security_kind"] == "cryptocurrency"
    assert btc["asset"] == {"kind": "security", "symbol": "btc"}
    assert btc["unit_value_quanta"] == "7500000"
    assert btc["current_value_quanta"] == "7500000"
    eth = positions_by_id["eth_holding"]
    assert eth["symbol"] == "eth"
    assert eth["security_kind"] == "cryptocurrency"
    assert eth["asset"] == {"kind": "security", "symbol": "eth"}
    assert eth["unit_value_quanta"] == "210000"
    assert eth["current_value_quanta"] == "1050000"
    pha = positions_by_id["private_holding_a"]
    assert pha["symbol"] == "PHA"
    assert pha.get("security_kind") is None
    assert pha["asset"] == {"kind": "private_equity", "issuer_id": "private_holding_a"}
    assert pha["unit_value_quanta"] == "2500"
    assert pha["current_value_quanta"] == "2500000"
    assert pha["total_cost_basis_quanta"] == "500000"


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
        "monthly_spend": 300_000,
        "spend_index": "none",
        "funding_policy": {"sleeve_weights": []},
    }
    fan = _post_json(
        server_url,
        "/api/product/projections/metric_fan",
        {"scenario": scenario, "first_seed": 7, "rollout_count": 1, "metric": "net_worth", "percentiles": [50]},
    )

    assert fan["failed_count"] == 1
    assert "rollout_summaries" not in fan
    assert fan["monthly_metric_fan"]["month_index"] == [0, 1, 2, 3]
    # Month 0 = cash 250k + holdings 835.5k + PHA 25k + bonds 150k at face; failure zeros
    # subsequent months.
    assert fan["monthly_metric_fan"]["value_quanta"] == [_usd_quanta(value) for value in [1_260_500.0, 0.0, 0.0, 0.0]]
    assert fan["terminal_metric_percentiles"] == {"percentile": [50.0], "value_quanta": [_usd_quanta(0.0)]}

    detail = _post_json(server_url, "/api/product/projections/rollout", {"scenario": scenario, "seed": 7})

    assert detail["rollout"]["failed"] is True
    terminal = detail["rollout"]["terminal_metrics"]
    assert terminal["failed_month_index"] == 0
    assert terminal["cash_quanta"] == _usd_quanta(0.0)
    assert terminal["holding_value_quanta"] == _usd_quanta(0.0)
    assert terminal["net_worth_quanta"] == _usd_quanta(0.0)
    assert terminal["shortfall_quanta"] == _usd_quanta(300_000.0)
    columns = detail["rollout"]["monthly_metrics"]
    assert columns["month_index"] == [0, 1, 2, 3]
    assert columns["cash_quanta"] == [_usd_quanta(value) for value in [250_000.0, 0.0, 0.0, 0.0]]
    assert columns["holding_value_quanta"] == [_usd_quanta(value) for value in [835_500.0, 0.0, 0.0, 0.0]]
    assert columns["net_worth_quanta"] == [_usd_quanta(value) for value in [1_260_500.0, 0.0, 0.0, 0.0]]
    expense, failure = detail["rollout"]["events"]
    assert expense == {
        "month_index": 0,
        "amount_quanta": _usd_quanta(0.0),
        "kind": "monthly_expense",
        "amount_due_quanta": _usd_quanta(300_000.0),
        "amount_paid_quanta": _usd_quanta(0.0),
        "shortfall_quanta": _usd_quanta(300_000.0),
    }
    assert failure == {
        "month_index": 0,
        "amount_quanta": _usd_quanta(300_000.0),
        "kind": "failure",
        "amount_due_quanta": _usd_quanta(300_000.0),
        "amount_paid_quanta": _usd_quanta(0.0),
        "shortfall_quanta": _usd_quanta(300_000.0),
    }


def test_backend_server_product_zero_width_band_sells_exactly_the_required_spend(server_url: str) -> None:
    scenario = {
        "model_id": "current_model",
        "horizon_months": 1,
        "monthly_spend": 300_000,
        "spend_index": "none",
        # Floor and ceiling both at zero: the month is projected to end at 250k + 1.875k of bond
        # coupon - 300k = -48.125k, so the band raises exactly that shortfall and nothing more.
        # The coupon offset is the point — it is money the month is already going to receive, so
        # counting it is what stops the band selling assets to cover income it already has. Every
        # sellable holding sits in the target, and water-filling takes it all from the overweight
        # VOO sleeve.
        "funding_policy": {
            "sleeve_weights": [
                {"symbol": "VOO", "weight": 1},
                {"symbol": "btc", "weight": 1},
                {"symbol": "eth", "weight": 1},
            ]
        },
    }

    detail = _post_json(server_url, "/api/product/projections/rollout", {"scenario": scenario, "seed": 7})

    assert detail["rollout"]["failed"] is False
    columns = detail["rollout"]["monthly_metrics"]
    assert columns["cash_quanta"] == [_usd_quanta(value) for value in [250_000.0, 0.0]]
    assert columns["holding_value_quanta"][0] == _usd_quanta(835_500.0)
    assert int(columns["holding_value_quanta"][1]) > 0
    terminal = detail["rollout"]["terminal_metrics"]
    assert terminal["cash_quanta"] == _usd_quanta(0.0)
    assert terminal["shortfall_quanta"] == _usd_quanta(0.0)
    # Bonds are the third term now: net worth is liquid + home equity + PE + bonds, and with
    # cash at zero and no property the identity is holdings + PE + bonds.
    assert int(terminal["net_worth_quanta"]) == (
        int(columns["holding_value_quanta"][1])
        + int(columns["private_equity_value_quanta"][1])
        + int(columns["bond_value_quanta"][1])
    )
    sale, expense = detail["rollout"]["events"]
    assert sale == {
        "month_index": 0,
        "amount_quanta": _usd_quanta(48_125.0),
        "kind": "holding_sale",
        "asset": {"kind": "security", "symbol": "VOO"},
        "asset_label": "SP500 Proxy (VOO)",
        "units": pytest.approx(96.25),
        "proceeds_quanta": _usd_quanta(48_125.0),
        # 96.25 units out of the $400/unit lot.
        "cost_basis_quanta": _usd_quanta(38_500.0),
    }
    assert expense == {
        "month_index": 0,
        "amount_quanta": _usd_quanta(300_000.0),
        "kind": "monthly_expense",
        "amount_due_quanta": _usd_quanta(300_000.0),
        "amount_paid_quanta": _usd_quanta(300_000.0),
        "shortfall_quanta": _usd_quanta(0.0),
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
                "monthly_spend": 1_000,
                "spend_index": "none",
                "funding_policy": {"sleeve_weights": []},
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
    assert pe_event["mark_quanta"] == _usd_quanta(25.0)
    assert pe_event["forced_sale_fraction"] == pytest.approx(0.25)

    [sale] = [
        event
        for event in detail["rollout"]["events"]
        if event["kind"] == "holding_sale"
        and event["asset"] == {"kind": "private_equity", "issuer_id": "private_holding_a"}
    ]
    assert sale["units"] == pytest.approx(250.0)
    assert sale["proceeds_quanta"] == _usd_quanta(6_250.0)


def test_api_product_metric_fan_respects_private_equity_tender_capacity(
    capacity_limited_private_equity_client: TestClient,
) -> None:
    scenario = {
        "model_id": "current_model",
        "horizon_months": 2,
        "monthly_spend": 1_000,
        "spend_index": "none",
        "funding_policy": {"sleeve_weights": []},
        "pe_tender_policy": {"liquid_net_worth_floor": 1_200_000, "index_floor_to_inflation": False},
    }

    fan_response = capacity_limited_private_equity_client.post(
        "/api/product/projections/metric_fan",
        json={"scenario": scenario, "first_seed": 7, "rollout_count": 1, "metric": "cash", "percentiles": [50]},
    )

    assert fan_response.status_code == 200
    fan = fan_response.json()
    assert fan["model_id"] == "capacity_limited_pe_fixture"
    assert fan["terminal_metric_percentiles"] == {"percentile": [50.0], "value_quanta": [_usd_quanta(256_125.0)]}
    assert "rollout_summaries" not in fan

    rollout_response = capacity_limited_private_equity_client.post(
        "/api/product/projections/rollout", json={"scenario": scenario, "seed": 7}
    )

    assert rollout_response.status_code == 200
    detail = rollout_response.json()
    assert detail["rollout"]["terminal_metrics"]["cash_quanta"] == _usd_quanta(256_125.0)
    assert detail["rollout"]["terminal_metrics"]["private_equity_value_quanta"] == _usd_quanta(18_750.0)
    [opportunity] = [event for event in detail["rollout"]["events"] if event["kind"] == "private_equity_opportunity"]
    assert opportunity["event_kind"] == "tender"
    assert opportunity["outcome"] == "sold"
    assert opportunity["sellable_units"] == pytest.approx(250.0)
    assert opportunity["target_units"] == pytest.approx(250.0)
    assert opportunity["proceeds_quanta"] == _usd_quanta(6_250.0)


def test_backend_server_product_cash_band_refills_to_the_ceiling_from_the_overweight_sleeve(server_url: str) -> None:
    scenario = {
        "model_id": "current_model",
        "horizon_months": 1,
        "monthly_spend": 1_000,
        "spend_index": "none",
        # The month is projected to end at 250k - 1k = 249k, under the 260k floor, so the band
        # raises 280k - 249k = 31k. VOO ($750k) and btc ($75k) carry the same target weight, so
        # VOO is the overweight sleeve and funds the whole raise on its own.
        "funding_policy": {
            "cash_floor": 260_000,
            "cash_ceiling": 280_000,
            # Nominal bounds: an inflation-indexed band would move with the sampled CPI path and
            # the refill would no longer be an exact number.
            "cash_band_index_to_inflation": False,
            "sleeve_weights": [{"symbol": "VOO", "weight": 1}, {"symbol": "btc", "weight": 1}],
        },
    }

    detail = _post_json(server_url, "/api/product/projections/rollout", {"scenario": scenario, "seed": 7})

    assert detail["rollout"]["failed"] is False
    columns = detail["rollout"]["monthly_metrics"]
    # Landing on the ceiling, not back on the floor: refilling to the floor would put the owner
    # right back at the trigger next month.
    assert columns["cash_quanta"] == [_usd_quanta(value) for value in [250_000.0, 280_000.0]]
    assert detail["rollout"]["terminal_metrics"]["cash_quanta"] == _usd_quanta(280_000.0)
    assert detail["rollout"]["terminal_metrics"]["shortfall_quanta"] == _usd_quanta(0.0)
    # Exactly two events: btc is inside the target but underweight, so it is not touched.
    sale, expense = detail["rollout"]["events"]
    assert sale == {
        "month_index": 0,
        "amount_quanta": _usd_quanta(29_125.0),
        "kind": "holding_sale",
        "asset": {"kind": "security", "symbol": "VOO"},
        "asset_label": "SP500 Proxy (VOO)",
        # 62 units at the $500 anchor, FIFO out of the $400/unit 2020 lot.
        "units": pytest.approx(58.25),
        "proceeds_quanta": _usd_quanta(29_125.0),
        "cost_basis_quanta": _usd_quanta(23_300.0),
    }
    assert expense["kind"] == "monthly_expense"
    assert expense["amount_paid_quanta"] == _usd_quanta(1_000.0)


def test_backend_server_product_rollout_includes_zero_tax_accrual_events_without_taxable_income(
    server_url: str,
) -> None:
    scenario = {
        "model_id": "current_model",
        "horizon_months": 12,
        "monthly_spend": 1_000,
        "spend_index": "none",
        "funding_policy": {"sleeve_weights": []},
    }

    detail = _post_json(server_url, "/api/product/projections/rollout", {"scenario": scenario, "seed": 7})

    tax_accruals = [event for event in detail["rollout"]["events"] if event["kind"] == "tax_accrual"]
    assert {event["jurisdiction_id"] for event in tax_accruals} == {"federal_us", "california"}
    assert {event["month_index"] for event in tax_accruals} == {11}
    assert all(event["amount_quanta"] == _usd_quanta(0.0) for event in tax_accruals)
    assert [event for event in detail["rollout"]["events"] if event["kind"] == "tax_payment"] == []


def test_backend_server_product_rollout_includes_federal_and_california_tax_events_for_holding_sales(
    server_url: str,
) -> None:
    scenario = {
        "model_id": "current_model",
        "horizon_months": 13,
        "monthly_spend": 1_000,
        "spend_index": "none",
        # A band wide enough that the month-0 refill (760k - 249k = 511k) sells past the whole
        # 2020 VOO lot, realizing a long-term gain big enough for both jurisdictions to charge.
        "funding_policy": {
            "cash_floor": 260_000,
            "cash_ceiling": 760_000,
            "cash_band_index_to_inflation": False,
            "sleeve_weights": [{"symbol": "VOO", "weight": 1}],
        },
    }

    detail = _post_json(server_url, "/api/product/projections/rollout", {"scenario": scenario, "seed": 7})

    events = detail["rollout"]["events"]
    tax_accruals = [event for event in events if event["kind"] == "tax_accrual"]
    assert {event["jurisdiction_id"] for event in tax_accruals} == {"federal_us", "california"}
    assert {event["month_index"] for event in tax_accruals} == {11}
    assert all(int(event["amount_quanta"]) > 0 for event in tax_accruals)
    assert sum(int(event["amount_quanta"]) for event in tax_accruals) == sum(
        int(event["total_tax_quanta"]) for event in tax_accruals
    )
    federal = next(event for event in tax_accruals if event["jurisdiction_id"] == "federal_us")
    california = next(event for event in tax_accruals if event["jurisdiction_id"] == "california")
    assert int(federal["capital_gain_tax_quanta"]) > 0
    assert california["capital_gain_tax_quanta"] == _usd_quanta(0.0)
    assert int(california["ordinary_tax_quanta"]) > 0

    tax_payments = [event for event in events if event["kind"] == "tax_payment"]
    [tax_payment] = tax_payments
    assert tax_payment["month_index"] == 12
    assert tax_payment["obligation_type"] == "tax_true_up"
    assert int(tax_payment["amount_due_quanta"]) == sum(int(event["amount_quanta"]) for event in tax_accruals)
    assert tax_payment["amount_paid_quanta"] == tax_payment["amount_due_quanta"]
    assert tax_payment["shortfall_quanta"] == _usd_quanta(0.0)


def test_backend_server_product_outside_rent_re_pegs_yearly(server_url: str) -> None:
    scenario = {
        "model_id": "current_model",
        "horizon_months": 13,
        "monthly_spend": 1000,
        "spend_index": "none",
        "monthly_rent": 3000,
        "rental_location_id": "location_a",
        "funding_policy": {"sleeve_weights": []},
    }

    detail = _post_json(server_url, "/api/product/projections/rollout", {"scenario": scenario, "seed": 7})

    rent_events = [event for event in detail["rollout"]["events"] if event["kind"] == "outside_rent"]
    assert len(rent_events) == 13
    year_zero = [event for event in rent_events if event["month_index"] < 12]
    assert {event["amount_paid_quanta"] for event in year_zero} == {_usd_quanta(3000.0)}
    [year_one_event] = [event for event in rent_events if event["month_index"] == 12]
    assert year_one_event["amount_paid_quanta"] != _usd_quanta(3000.0)


def test_backend_server_product_rent_rejects_unknown_location(server_url: str) -> None:
    scenario = {
        "model_id": "current_model",
        "horizon_months": 3,
        "monthly_spend": 1000,
        "spend_index": "none",
        "monthly_rent": 3000,
        "rental_location_id": "not_a_real_location",
        "funding_policy": {"sleeve_weights": []},
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
        "monthly_spend": 1_000,
        "spend_index": "none",
        "pe_tender_policy": {"liquid_net_worth_floor": 5_000_000, "index_floor_to_inflation": False},
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
    assert int(sale["proceeds_quanta"]) > 0
    assert sale["units"] > 0


if __name__ == "__main__":
    pytest_bazel.main()
