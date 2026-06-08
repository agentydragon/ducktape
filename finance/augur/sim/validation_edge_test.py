"""Focused validation and edge-behavior tests for the JAX sim engine."""

from __future__ import annotations

import numpy as np
import polars as pl
import pytest
import pytest_bazel

from finance.augur.model.private_equity_bundle import PrivateEquityBundle
from finance.augur.model.series import CryptoSymbol, IssuerId, PrivateEquityEventKindCode, PrivateEquityRegimeCode
from finance.augur.product.asset_key import CryptoAssetKey, PrivateEquityAssetKey, SP500AssetKey
from finance.augur.sim.external_series import EXTERNAL_SERIES_VALUES_FRAME, ExternalSeriesContext
from finance.augur.sim.scenario import (
    Agent,
    FilingStatus,
    HarvestPolicy,
    InitialAccountBalance,
    InitialLot,
    LiquidityPolicy,
    Scenario,
    ScheduledAssetSale,
    ScheduledObligation,
    TaxProfile,
)
from finance.augur.sim.simulate import simulate, simulate_with_external_series
from finance.augur.sim.tlh_harvest import HarvestYieldParams


def _external_series_context_for_levels(series_id: str, levels_by_rollout: list[list[float]]) -> ExternalSeriesContext:
    return ExternalSeriesContext(
        series_values=EXTERNAL_SERIES_VALUES_FRAME.normalize(
            pl.DataFrame(
                [
                    {"rollout_index": rollout, "month_index": month, "series_id": series_id, "value": level}
                    for rollout, levels in enumerate(levels_by_rollout)
                    for month, level in enumerate(levels)
                ]
            )
        )
    )


def _pe_validation_scenario(*, horizon_months: int) -> Scenario:
    return Scenario(
        agents=[Agent(agent_id="alice")],
        initial_cash=[InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=0.0)],
        initial_lots=[
            InitialLot(
                lot_id="acme_lot",
                agent_id="alice",
                account_id="checking",
                asset=PrivateEquityAssetKey(issuer_id=IssuerId("acme")),
                purchase_month_index=-36,
                quantity=100.0,
                cost_basis_per_unit_usd=10.0,
            )
        ],
        tax_profiles=[],
        horizon_months=horizon_months,
    )


def _pe_external_with_channel_value(
    *, channel: str, month: int, value: float, horizon_months: int
) -> ExternalSeriesContext:
    rollouts = 1
    shape = (rollouts, horizon_months + 1)
    tender = np.zeros(shape, dtype=np.bool_)
    valid = PrivateEquityBundle.from_issuer_arrays(
        "acme",
        mark_usd_per_unit=np.full(shape, 100.0, dtype=np.float64),
        regime_code=np.full(shape, int(PrivateEquityRegimeCode.PRIVATE_OPERATING), dtype=np.int64),
        event_kind_code=np.full(shape, int(PrivateEquityEventKindCode.NONE), dtype=np.int64),
        sale_opportunity_active=tender,
        sale_capacity_fraction=np.ones(shape, dtype=np.float64),
        eligible_fraction=np.ones(shape, dtype=np.float64),
        forced_sale_fraction=np.zeros(shape, dtype=np.float64),
        liquidity_blocked=np.zeros(shape, dtype=np.bool_),
        forced_recovery_cashout_usd=np.zeros(shape, dtype=np.float64),
        company_valuation_usd=np.zeros(shape, dtype=np.float64),
        rollout_count=rollouts,
        horizon_months=horizon_months,
    )
    patched = valid.frame.with_columns(
        pl.when((pl.col("rollout_index") == 0) & (pl.col("month_index") == month))
        .then(pl.lit(value, dtype=pl.Float64))
        .otherwise(pl.col(channel))
        .alias(channel)
    )
    return ExternalSeriesContext(
        series_values=EXTERNAL_SERIES_VALUES_FRAME.empty(), private_equity=PrivateEquityBundle(patched)
    )


@pytest.mark.parametrize(
    ("channel", "bad_value", "match"),
    [
        (
            "mark_usd_per_unit",
            -1.0,
            r"private-equity mark series for issuer 'acme' produced a negative or non-finite value",
        ),
        (
            "mark_usd_per_unit",
            float("nan"),
            r"private-equity mark series for issuer 'acme' produced a negative or non-finite value",
        ),
        (
            "forced_recovery_cashout_usd",
            -1.0,
            r"private-equity forced-recovery cashout series produced a negative value",
        ),
    ],
    ids=["pe-negative-mark", "pe-nonfinite-mark", "pe-negative-recovery"],
)
def test_private_equity_sampled_channel_validation(channel: str, bad_value: float, match: str) -> None:
    horizon = 2
    scenario = _pe_validation_scenario(horizon_months=horizon)
    external = _pe_external_with_channel_value(channel=channel, month=1, value=bad_value, horizon_months=horizon)

    with pytest.raises(ValueError, match=match):
        simulate_with_external_series(scenario, rollout_count=1, external_series=external, locations={})


def test_private_equity_terminal_snapshot_is_not_validated_as_a_sim_month() -> None:
    horizon = 2
    scenario = _pe_validation_scenario(horizon_months=horizon)
    external = _pe_external_with_channel_value(
        channel="mark_usd_per_unit", month=horizon, value=-1.0, horizon_months=horizon
    )

    result = simulate_with_external_series(scenario, rollout_count=1, external_series=external, locations={})

    assert result.events_log.rollout_failures.is_empty()


def test_tlh_terminal_snapshot_is_not_validated_as_a_sim_month() -> None:
    horizon = 2
    scenario = Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="irs")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=0.0),
            InitialAccountBalance(agent_id="irs", account_id="checking", balance_usd=0.0),
        ],
        initial_lots=[
            InitialLot(
                lot_id="alice_sp500",
                agent_id="alice",
                account_id="brokerage",
                asset=SP500AssetKey(),
                purchase_month_index=0,
                quantity=100.0,
                cost_basis_per_unit_usd=1.0,
            )
        ],
        harvest_policies=[
            HarvestPolicy(
                owner_agent_id="alice",
                account_id="brokerage",
                asset=SP500AssetKey(),
                yield_params=HarvestYieldParams(
                    peak_annual_yield=0.12,
                    floor_annual_yield=0.004,
                    maturity_decay_exponent=1.5,
                    drawdown_sensitivity=6.0,
                ),
            )
        ],
        tax_profiles=[
            TaxProfile(
                agent_id="alice",
                filing_status=FilingStatus.SINGLE,
                jurisdiction_ids=["federal_us"],
                tax_authority_agent_id="irs",
            )
        ],
        horizon_months=horizon,
    )
    external = _external_series_context_for_levels("sp500", [[1.0, 1.0, -1.0]])

    result = simulate_with_external_series(scenario, rollout_count=1, external_series=external, locations={})

    assert result.events_log.rollout_failures.is_empty()


def test_scheduled_sale_oversell_validation() -> None:
    scenario = Scenario(
        agents=[Agent(agent_id="alice")],
        initial_cash=[InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=0.0)],
        initial_lots=[
            InitialLot(
                lot_id="taxable_vti",
                agent_id="alice",
                account_id="taxable",
                asset=CryptoAssetKey(symbol=CryptoSymbol("vti")),
                purchase_month_index=-12,
                quantity=5.0,
                cost_basis_per_unit_usd=80.0,
            )
        ],
        scheduled_asset_sales=[
            ScheduledAssetSale(
                month=1,
                cause_id="oversell",
                agent_id="alice",
                source_account_id="taxable",
                asset=CryptoAssetKey(symbol=CryptoSymbol("vti")),
                quantity=6.0,
                price_per_unit_usd=100.0,
                proceeds_account_id="checking",
            )
        ],
        tax_profiles=[],
        horizon_months=2,
    )

    with pytest.raises(ValueError, match=r"scheduled asset sale exceeds available lots"):
        simulate(scenario, rollout_count=1, locations={})


@pytest.mark.parametrize("bad_price", [0.0, -100.0, float("nan")], ids=["zero", "negative", "nonfinite"])
def test_liquidity_invalid_asset_price_leaves_obligation_unfunded(bad_price: float) -> None:
    scenario = Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="landlord")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=0.0),
            InitialAccountBalance(agent_id="landlord", account_id="checking", balance_usd=0.0),
        ],
        initial_lots=[
            InitialLot(
                lot_id="alice_vti",
                agent_id="alice",
                account_id="checking",
                asset=CryptoAssetKey(symbol=CryptoSymbol("vti")),
                purchase_month_index=-24,
                quantity=10.0,
                cost_basis_per_unit_usd=50.0,
            )
        ],
        scheduled_obligations=[
            ScheduledObligation(
                month=0,
                obligation_id="rent_due",
                obligation_type="rent",
                agent_id="alice",
                from_account_id="checking",
                to_agent_id="landlord",
                to_account_id="checking",
                amount_due_usd=500.0,
            )
        ],
        liquidity_policies=[
            LiquidityPolicy(
                agent_id="alice",
                account_id="checking",
                asset_preference_chain=[CryptoAssetKey(symbol=CryptoSymbol("vti"))],
            )
        ],
        tax_profiles=[],
        horizon_months=1,
    )
    external = _external_series_context_for_levels("crypto:vti", [[bad_price, bad_price]])

    result = simulate_with_external_series(scenario, rollout_count=1, external_series=external, locations={})

    assert result.events_log.lot_dispositions.is_empty()
    assert result.events_log.rollout_failures.row(0, named=True)["month_index"] == 0
    assert result.rollout_status.row(0, named=True)["status"] == "failed_insufficient_cash"


if __name__ == "__main__":
    pytest_bazel.main()
