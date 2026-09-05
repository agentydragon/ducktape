"""Rust/JAX differential coverage for stateful reduced-form tax-loss harvesting and its
sale-time give-back.

One target per domain so Bazel runs them concurrently: each case compiles its own
JAX program, and those compiles are the suite's whole wall clock and peak memory.
"""

from decimal import Decimal

import polars as pl
import pytest_bazel

from finance.augur.model.series import InflationKey, SecurityKey, SecuritySymbol
from finance.augur.rust.differential.backend import RustResult, assert_backends_agree
from finance.augur.rust.differential.fixtures import checking, taxed
from finance.augur.sim.scenario import (
    HarvestPolicy,
    InitialAccountBalance,
    InitialLot,
    ObligationType,
    ScheduledAssetSale,
    ScheduledObligation,
    SeriesIndexedAmount,
    SleeveTarget,
    TargetAllocationPolicy,
)
from finance.augur.sim.testing.case import Case, levels, scenario
from finance.augur.sim.tlh_harvest import HarvestYieldParams

SP500 = SecurityKey(symbol=SecuritySymbol("sp500"))
INFLATION = InflationKey()
HORIZON_MONTHS = 12

# A drawdown path and a flat one: the curve harvests more where the position fell.
DRAWDOWN_PATH = [
    Decimal(1),
    Decimal(1),
    Decimal("0.80"),
    Decimal("0.80"),
    Decimal("0.90"),
    Decimal("0.90"),
    Decimal("0.90"),
    *([Decimal("0.95")] * 6),
]
FLAT_PATH = [Decimal(1)] * (HORIZON_MONTHS + 1)


def _sale(cause_id: str, *, month: int, quantity: float) -> ScheduledAssetSale:
    return ScheduledAssetSale(
        month=month,
        cause_id=cause_id,
        agent_id="alice",
        source_account_id="brokerage",
        asset=SP500,
        quantity=quantity,
        proceeds_account_id="checking",
    )


def tlh_case(
    *,
    partial_sales: bool = False,
    same_month_sales: bool = False,
    target_allocation_sale: bool = False,
    failure_after_first_harvest: bool = False,
) -> Case:
    """A harvested index position, and the events that give its deferred basis back."""

    scheduled_asset_sales: list[ScheduledAssetSale] = []
    if partial_sales:
        scheduled_asset_sales = [
            _sale("sp500_half", month=4, quantity=500.0),
            _sale("sp500_rest", month=7, quantity=500.0),
        ]
    if same_month_sales:
        scheduled_asset_sales = [
            _sale("sp500_quarter_a", month=4, quantity=250.0),
            _sale("sp500_quarter_b", month=4, quantity=250.0),
            _sale("sp500_final_half", month=7, quantity=500.0),
        ]
    accounts = [
        InitialAccountBalance(
            agent_id="alice", account_id="brokerage", balance=Decimal(5) if target_allocation_sale else Decimal(0)
        ),
        *checking(("alice", Decimal(0)), ("irs", Decimal(0))),
    ]
    scheduled_obligations: list[ScheduledObligation] = []
    if failure_after_first_harvest:
        accounts.extend(checking(("sink", Decimal(0))))
        scheduled_obligations = [
            ScheduledObligation(
                month=1,
                obligation_id="unfunded_after_harvest",
                obligation_type=ObligationType.CASH_SPEND,
                agent_id="alice",
                from_account_id="brokerage",
                to_agent_id="sink",
                to_account_id="checking",
                amount_due=Decimal("0.01"),
            )
        ]
    target_allocation_policies: list[TargetAllocationPolicy] = []
    if target_allocation_sale:
        target_allocation_policies = [
            TargetAllocationPolicy(
                agent_id="alice",
                account_id="brokerage",
                source_account_ids=("brokerage",),
                sleeves=[SleeveTarget(asset=SP500, weight=1)],
                cash_floor=SeriesIndexedAmount(base_amount=Decimal(5), series=INFLATION),
                cash_ceiling=Decimal(20),
            )
        ]
    branching = partial_sales or same_month_sales or target_allocation_sale
    prices = [FLAT_PATH] if branching else [DRAWDOWN_PATH, FLAT_PATH]
    return Case(
        scenario=scenario(
            accounts,
            horizon_months=HORIZON_MONTHS,
            initial_lots=[
                InitialLot(
                    lot_id="alice_sp500",
                    agent_id="alice",
                    account_id="brokerage",
                    asset=SP500,
                    purchase_month_index=0,
                    quantity=1_000.0,
                    cost_basis_per_unit=Decimal(1),
                )
            ],
            scheduled_asset_sales=scheduled_asset_sales,
            scheduled_obligations=scheduled_obligations,
            target_allocation_policies=target_allocation_policies,
            harvest_policies=[
                HarvestPolicy(
                    owner_agent_id="alice",
                    account_id="brokerage",
                    asset=SP500,
                    yield_params=HarvestYieldParams(
                        peak_annual_yield=0.12,
                        floor_annual_yield=0.004,
                        maturity_decay_exponent=1.5,
                        drawdown_sensitivity=6.0,
                    ),
                    short_term_fraction=1.0,
                )
            ],
            tax_profiles=[taxed("alice", "federal_us")],
        ),
        rollout_count=len(prices),
        series={
            SP500: levels(prices),
            **({INFLATION: levels([[Decimal(1), *([Decimal(2)] * HORIZON_MONTHS)]])} if target_allocation_sale else {}),
        },
    )


def _ledger(result: RustResult, rollout: int, month: int) -> int:
    """The single harvest policy's cumulative deferral at one snapshot."""

    row = result.tlh_ledger.filter(
        (pl.col("rollout_index") == rollout) & (pl.col("month_index") == month) & (pl.col("policy_index") == 0)
    )
    return int(row.get_column("cumulative_harvest_quanta").item())


def _assert_ledger_mirrors_short_term_gains(result: RustResult) -> None:
    """Before the first year-end, the deferral ledger is exactly the short-term loss booked.

    This is the invariant tying the two representations together: what the harvest policy
    accumulates is what the taxpayer's short-term gain reflects, with the sign flipped.
    """

    short_term = {
        (row["rollout_index"], row["month_index"]): row["gain_quanta"]
        for row in result.capital_gains.filter(pl.col("classification") == "stcg").to_dicts()
    }
    pre_year_end = result.tlh_ledger.filter(pl.col("month_index") < 12)
    assert not pre_year_end.is_empty()
    for row in pre_year_end.to_dicts():
        gain = short_term.get((row["rollout_index"], row["month_index"]), 0)
        assert row["cumulative_harvest_quanta"] == -gain


def test_backends_agree_on_harvest_paths_and_year_end_tax() -> None:
    result = assert_backends_agree(tlh_case())
    _assert_ledger_mirrors_short_term_gains(result)

    # The drawdown path harvests more than the flat one, and both harvest something.
    assert _ledger(result, 0, 3) > _ledger(result, 1, 3) > 0


def test_backends_agree_that_a_partial_sale_gives_basis_back() -> None:
    result = assert_backends_agree(tlh_case(partial_sales=True))
    _assert_ledger_mirrors_short_term_gains(result)

    assert _ledger(result, 0, 8) == 0


def test_backends_agree_that_same_month_sales_share_the_pre_sale_ledger() -> None:
    result = assert_backends_agree(tlh_case(same_month_sales=True))
    _assert_ledger_mirrors_short_term_gains(result)

    assert _ledger(result, 0, 8) == 0


def test_backends_agree_on_give_back_through_a_target_allocation_sale() -> None:
    result = assert_backends_agree(tlh_case(target_allocation_sale=True))

    dispositions = result.events.lot_dispositions
    assert dispositions.filter(pl.col("cause_id").str.starts_with("allocation_sale_m1_security:sp500")).height == 1
    assert _ledger(result, 0, 2) > 0


def test_backends_agree_that_failure_suppresses_the_harvest_ledger() -> None:
    result = assert_backends_agree(tlh_case(failure_after_first_harvest=True))

    assert result.rollout_status.get_column("failed_month").unique().to_list() == [1]
    for rollout in result.tlh_ledger.get_column("rollout_index").unique():
        assert _ledger(result, rollout, 1) > 0
        frozen = result.tlh_ledger.filter((pl.col("rollout_index") == rollout) & (pl.col("month_index") > 1))
        assert frozen.get_column("cumulative_harvest_quanta").unique().to_list() == [0]


if __name__ == "__main__":
    pytest_bazel.main()
