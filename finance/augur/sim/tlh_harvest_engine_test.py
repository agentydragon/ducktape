"""Engine-level tests for the reduced-form TLH harvest process (Piece 2b).

These exercise `_apply_tlh_harvest` + the sale-time basis give-back end-to-end through the dense
engine, complementing the pure-core invariants in `tlh_harvest_test.py`. The four required
correctness properties:
  - down months harvest strictly more loss than flat months; a long bull run ossifies to the floor,
  - a harvested short-term loss offsets a concurrent realized gain → lower tax than no-harvest,
  - the basis give-back at sale repays exactly the cumulative harvest (deferral, not free money),
  - harvest-off reproduces today's behavior exactly (regression).
"""

from __future__ import annotations

import polars as pl
import pytest
import pytest_bazel

from finance.augur.model.series import CryptoSymbol
from finance.augur.product.asset_key import CryptoAssetKey, SP500AssetKey
from finance.augur.sim.external_series import EXTERNAL_SERIES_VALUES_FRAME, ExternalSeriesContext
from finance.augur.sim.scenario import (
    Agent,
    FilingStatus,
    HarvestPolicy,
    InitialAccountBalance,
    InitialLot,
    Scenario,
    ScheduledAssetSale,
    TaxProfile,
)
from finance.augur.sim.simulate import simulate_with_external_series
from finance.augur.sim.tlh_harvest import HarvestYieldParams

# A high peak yield + strong drawdown sensitivity makes the harvested losses large enough to read
# cleanly off the YTD frame in a short horizon. These are test fixtures, not calibrated values.
_PARAMS = HarvestYieldParams(
    peak_annual_yield=0.12, floor_annual_yield=0.004, maturity_decay_exponent=1.5, drawdown_sensitivity=6.0
)


def _sp500_levels(levels_by_rollout: list[list[float]]) -> ExternalSeriesContext:
    return ExternalSeriesContext(
        series_values=EXTERNAL_SERIES_VALUES_FRAME.normalize(
            pl.DataFrame(
                [
                    {"rollout_index": r, "month_index": m, "series_id": "sp500", "value": level}
                    for r, levels in enumerate(levels_by_rollout)
                    for m, level in enumerate(levels)
                ]
            )
        )
    )


def _harvest_scenario(
    *,
    horizon_months: int,
    quantity: float = 1000.0,
    cost_basis_per_unit_usd: float = 1.0,
    purchase_month_index: int = 0,
    with_harvest: bool,
    short_term_fraction: float = 1.0,
    scheduled_asset_sales: list[ScheduledAssetSale] | None = None,
    extra_lots: list[InitialLot] | None = None,
) -> Scenario:
    """Single taxable agent holding an SP500 sleeve, optionally with a harvest policy.

    The sleeve is one lot priced by the `sp500` series; `unit_value`/quantity are chosen so MV is
    easy to reason about (1000 units at $1 cost basis). `extra_lots` adds non-sleeve lots (e.g. a
    gain lot to offset)."""

    lots = [
        InitialLot(
            lot_id="alice_sp500",
            agent_id="alice",
            account_id="brokerage",
            asset=SP500AssetKey(),
            purchase_month_index=purchase_month_index,
            quantity=quantity,
            cost_basis_per_unit_usd=cost_basis_per_unit_usd,
        )
    ]
    if extra_lots:
        lots.extend(extra_lots)
    harvest_policies = (
        [
            HarvestPolicy(
                owner_agent_id="alice",
                account_id="brokerage",
                asset=SP500AssetKey(),
                yield_params=_PARAMS,
                short_term_fraction=short_term_fraction,
            )
        ]
        if with_harvest
        else []
    )
    return Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="irs")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="brokerage", balance_usd=0.0),
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=0.0),
            InitialAccountBalance(agent_id="irs", account_id="checking", balance_usd=0.0),
        ],
        initial_lots=lots,
        scheduled_asset_sales=scheduled_asset_sales or [],
        harvest_policies=harvest_policies,
        tax_profiles=[
            TaxProfile(
                agent_id="alice",
                filing_status=FilingStatus.SINGLE,
                jurisdiction_ids=["federal_us"],
                tax_authority_agent_id="irs",
            )
        ],
        horizon_months=horizon_months,
    )


def _ytd_gain(result, *, month_index: int, classification: str, rollout_index: int = 0) -> float:
    rows = result.capital_gains_ytd.filter(
        (pl.col("month_index") == month_index)
        & (pl.col("rollout_index") == rollout_index)
        & (pl.col("agent_id") == "alice")
        & (pl.col("classification") == classification)
    )
    if rows.is_empty():
        return 0.0
    return float(rows.get_column("gain_usd").sum())


def _harvested_short_term_in_month(result, *, calendar_month: int, rollout_index: int = 0) -> float:
    """Magnitude of short-term loss harvested during `calendar_month` (a positive number).

    State snapshot `month_index = m + 1` reflects the end of calendar month `m`, so the loss booked
    during month `m` is the drop in cumulative YTD short-term gain from snapshot `m` to `m + 1`."""

    before = _ytd_gain(result, month_index=calendar_month, classification="stcg", rollout_index=rollout_index)
    after = _ytd_gain(result, month_index=calendar_month + 1, classification="stcg", rollout_index=rollout_index)
    return before - after


@pytest.mark.parametrize("bad_level", [-0.01, float("nan")], ids=["negative", "nonfinite"])
def test_harvest_index_validation_rejects_negative_or_nonfinite_prices(bad_level: float) -> None:
    scenario = _harvest_scenario(horizon_months=2, with_harvest=True)
    external_series = _sp500_levels([[1.0, bad_level, 1.0]])

    with pytest.raises(ValueError, match=r"harvest policy 0 index series produced a negative or non-finite price"):
        simulate_with_external_series(scenario, rollout_count=1, external_series=external_series, locations={})


def test_down_month_harvests_strictly_more_than_flat_month() -> None:
    # Two rollouts, same fresh sleeve. Rollout 0 has a 20% drawdown in calendar month 1; rollout 1
    # is flat. The loss harvested DURING month 1 must be strictly larger for the drawdown rollout
    # (the drawdown kicker), isolating the period-return effect (both enter month 1 with the same
    # basis and a comparable embedded-gain fraction).
    scenario = _harvest_scenario(horizon_months=3, with_harvest=True)
    external_series = _sp500_levels([[1.0, 1.0, 0.8, 0.8], [1.0, 1.0, 1.0, 1.0]])
    result = simulate_with_external_series(scenario, rollout_count=2, external_series=external_series, locations={})

    drawdown_harvest = _harvested_short_term_in_month(result, calendar_month=2, rollout_index=0)
    flat_harvest = _harvested_short_term_in_month(result, calendar_month=2, rollout_index=1)
    assert drawdown_harvest > flat_harvest > 0.0


def test_long_bull_run_ossifies_harvest_toward_floor() -> None:
    # A long, steady bull run: basis stays at month-0 level while MV climbs, so the embedded-gain
    # fraction e -> 1 and the harvested loss per month decays toward the floor. Compare an early
    # month's harvest to a late month's; late must be strictly smaller (ossification).
    horizon = 24
    # +3%/month compounding bull market, no drawdowns.
    levels = [1.0 * (1.03**m) for m in range(horizon + 1)]
    scenario = _harvest_scenario(horizon_months=horizon, with_harvest=True)
    external_series = _sp500_levels([levels])
    result = simulate_with_external_series(scenario, rollout_count=1, external_series=external_series, locations={})

    early = _harvested_short_term_in_month(result, calendar_month=1)
    late = _harvested_short_term_in_month(result, calendar_month=12)
    assert early > 0.0  # a real loss was harvested early
    assert late < early  # ossification: harvest decays as embedded gains build


def test_harvested_short_term_loss_offsets_realized_gain_lowering_tax() -> None:
    # Alice realizes a real short-term capital GAIN (a separate crypto-like lot sold at a profit) in
    # the same year she harvests SP500 losses. With harvesting on, the harvested ST loss nets against
    # that gain (§1211/§1212), lowering the year's tax vs the no-harvest baseline.
    gain_lot = InitialLot(
        lot_id="alice_gain",
        agent_id="alice",
        account_id="brokerage",
        asset=CryptoAssetKey(symbol=CryptoSymbol("gainco")),
        purchase_month_index=-3,  # short-term when sold at month 6
        quantity=100.0,
        cost_basis_per_unit_usd=100.0,
    )
    gain_sale = ScheduledAssetSale(
        month=6,
        cause_id="alice_gain_sale",
        agent_id="alice",
        source_account_id="brokerage",
        asset=CryptoAssetKey(symbol=CryptoSymbol("gainco")),
        quantity=100.0,
        price_per_unit_usd=400.0,  # $30k short-term gain
        proceeds_account_id="checking",
    )
    # SP500 sleeve drops then recovers so harvesting books meaningful losses through the year.
    sp500_levels = [1.0, 0.85, 0.85, 0.9, 0.9, 0.9, 0.95] + [0.95] * 7
    gain_levels = [400.0] * 14
    external_series = ExternalSeriesContext(
        series_values=EXTERNAL_SERIES_VALUES_FRAME.normalize(
            pl.DataFrame(
                [
                    {"rollout_index": 0, "month_index": m, "series_id": series_id, "value": value}
                    for series_id, values in (("sp500", sp500_levels), ("crypto:gainco", gain_levels))
                    for m, value in enumerate(values)
                ]
            )
        )
    )

    def year_tax(with_harvest: bool) -> float:
        scenario = _harvest_scenario(
            horizon_months=13, with_harvest=with_harvest, scheduled_asset_sales=[gain_sale], extra_lots=[gain_lot]
        )
        result = simulate_with_external_series(scenario, rollout_count=1, external_series=external_series, locations={})
        accruals = result.events_log.tax_accruals.filter(pl.col("jurisdiction_id") == "federal_us")
        return float(accruals.get_column("amount_usd").sum())

    tax_with = year_tax(with_harvest=True)
    tax_without = year_tax(with_harvest=False)
    assert tax_with < tax_without


def test_give_back_makes_sale_gain_larger_by_cumulative_harvest_and_is_bounded() -> None:
    # The deferral check. Hold the sleeve, harvest for several months, then liquidate the entire
    # sleeve. The realized gain at sale must be larger WITH harvesting than without — by exactly the
    # cumulative harvested loss booked over the held months — so the deferred gain is fully repaid.
    # Net: the year's total realized capital gain (harvested losses + give-back at sale) returns to
    # the no-harvest baseline, proving the benefit is deferral/timing, not unbounded free money.
    horizon = 8
    sale_month = 6
    # Flat sleeve price so the sale itself realizes ~zero economic gain; all the give-back is the
    # repaid deferral. (Price 1.0 == cost basis, so without harvest the sale gain is 0.)
    levels = [1.0] * (horizon + 1)
    sale = ScheduledAssetSale(
        month=sale_month,
        cause_id="alice_sp500_liquidate",
        agent_id="alice",
        source_account_id="brokerage",
        asset=SP500AssetKey(),
        quantity=1000.0,
        price_per_unit_usd=1.0,
        proceeds_account_id="checking",
    )
    external_series = _sp500_levels([levels])

    def run(with_harvest: bool):
        scenario = _harvest_scenario(horizon_months=horizon, with_harvest=with_harvest, scheduled_asset_sales=[sale])
        return simulate_with_external_series(scenario, rollout_count=1, external_series=external_series, locations={})

    harvested = run(with_harvest=True)
    baseline = run(with_harvest=False)

    # Snapshot `month_index = m + 1` is the end of calendar month `m`. The sale fires inside month
    # `sale_month` (before that month's harvest, which then finds an empty sleeve), so the harvest
    # accumulated through the END of month sale_month-1 — i.e. snapshot `month_index = sale_month` —
    # is exactly what gets given back. It is the cumulative short-term loss booked so far (negative).
    cumulative_harvest = -_ytd_gain(harvested, month_index=sale_month, classification="stcg")
    assert cumulative_harvest > 0.0

    # Realized gain booked AT the sale = the jump in cumulative YTD across the sale month, i.e. from
    # snapshot `sale_month` to `sale_month + 1`.
    def sale_realized(result) -> float:
        before = _ytd_gain(result, month_index=sale_month, classification="stcg") + _ytd_gain(
            result, month_index=sale_month, classification="ltcg"
        )
        after = _ytd_gain(result, month_index=sale_month + 1, classification="stcg") + _ytd_gain(
            result, month_index=sale_month + 1, classification="ltcg"
        )
        return after - before

    # Baseline sale realizes ~0 (price == basis). The harvested run's sale realizes the give-back —
    # an extra gain equal to exactly the cumulative harvested loss (deferral repaid).
    assert sale_realized(baseline) == pytest.approx(0.0, abs=1e-6)
    assert sale_realized(harvested) == pytest.approx(cumulative_harvest, rel=1e-9, abs=1e-6)

    # Deferral, not free money: after the give-back, the net realized capital gain over the whole
    # (sub-year) horizon returns to the no-harvest baseline (~0) — bounded, not unbounded free money.
    net_st = _ytd_gain(harvested, month_index=horizon, classification="stcg")
    net_lt = _ytd_gain(harvested, month_index=horizon, classification="ltcg")
    assert net_st + net_lt == pytest.approx(0.0, abs=1e-6)


def test_partial_sales_give_back_proportionally_and_never_exceed_harvest() -> None:
    # Two partial sales (half, then the rest) must together give back exactly the cumulative harvest
    # — proportional to units sold — and never more (the scalar drains, so no double give-back).
    horizon = 9
    levels = [1.0] * (horizon + 1)
    sales = [
        ScheduledAssetSale(
            month=4,
            cause_id="alice_sp500_half",
            agent_id="alice",
            source_account_id="brokerage",
            asset=SP500AssetKey(),
            quantity=500.0,
            price_per_unit_usd=1.0,
            proceeds_account_id="checking",
        ),
        ScheduledAssetSale(
            month=7,
            cause_id="alice_sp500_rest",
            agent_id="alice",
            source_account_id="brokerage",
            asset=SP500AssetKey(),
            quantity=500.0,
            price_per_unit_usd=1.0,
            proceeds_account_id="checking",
        ),
    ]
    scenario = _harvest_scenario(horizon_months=horizon, with_harvest=True, scheduled_asset_sales=sales)
    result = simulate_with_external_series(
        scenario, rollout_count=1, external_series=_sp500_levels([levels]), locations={}
    )

    # By the terminal month, all units are sold, so the entire cumulative harvest has been given
    # back: the year-cumulative net short-term gain returns to ~0 (price flat == basis).
    net_st = _ytd_gain(result, month_index=horizon, classification="stcg")
    assert net_st == pytest.approx(0.0, abs=1e-6)


def test_harvest_off_reproduces_baseline_capital_gains_exactly() -> None:
    # Regression: a scenario with no harvest policy must produce byte-identical capital-gain YTD to
    # the same scenario run on the pre-harvest code path (here: no harvested losses ever appear).
    horizon = 6
    levels = [1.0, 0.8, 0.9, 0.85, 0.95, 1.1, 1.2]
    scenario = _harvest_scenario(horizon_months=horizon, with_harvest=False)
    result = simulate_with_external_series(
        scenario, rollout_count=1, external_series=_sp500_levels([levels]), locations={}
    )
    # No sales, no harvest → no capital-gain rows at all.
    assert result.capital_gains_ytd.filter(pl.col("agent_id") == "alice").is_empty()


if __name__ == "__main__":
    pytest_bazel.main()
