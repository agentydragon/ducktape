"""Sim-level e2e for scheduled asset purchases — the first way a tax lot comes into
existence mid-horizon.

Purchases matter beyond "sales with the sign flipped" because they make cost basis
PER-ROLLOUT for the first time. Every lot before this carried a basis fixed at compile
time; a lot bought in month 3 carries whatever its own rollout paid that month. Most of
the assertions below are really about that: if basis were still read from the plan's
static column, a purchased lot would report a basis of zero and every gain measured
against it would be the entire proceeds.

Prices are pinned rather than sampled, so each number is exact.
"""

from __future__ import annotations

import numpy as np
import polars as pl
import pytest_bazel

from finance.augur.model.deterministic import Deterministic
from finance.augur.model.level_series_groups import AssetPriceGroups
from finance.augur.model.series import SecuritySymbol
from finance.augur.model.series_model import SeriesModelBundle
from finance.augur.product.asset_key import SecurityKey
from finance.augur.product.decode import monthly_metric_arrays
from finance.augur.sim.scenario import (
    Agent,
    FilingStatus,
    InitialAccountBalance,
    Scenario,
    ScheduledAssetPurchase,
    ScheduledAssetSale,
    TaxProfile,
)
from finance.augur.sim.simulate import simulate

# Horizon stops short of the December year-end settlement, so cash assertions see the
# purchase and the sale and nothing else.
_HORIZON = 6
_ASSET = SecurityKey(symbol=SecuritySymbol("vti"))
_PRICE = 100.0
_SPEND = 500_000.0
_UNITS = 5_000.0
_OPENING_CASH = 1_000_000.0
# Flat through the month-1 purchase, then doubled — so a lot carried at cost is distinguishable
# from a lot carried at the mark.
_PRICE_PATH = [_PRICE] * 3 + [2 * _PRICE] * (_HORIZON + 1 - 3)


def _scenario(
    *, opening_cash: float = _OPENING_CASH, spend: float = _SPEND, sale_price: float | None = None, sale_month: int = 4
) -> Scenario:
    return Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="irs")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=opening_cash),
            InitialAccountBalance(agent_id="irs", account_id="checking", balance_usd=0.0),
        ],
        scheduled_asset_purchases=[
            ScheduledAssetPurchase(
                month=1,
                cause_id="buy_vti",
                lot_id="bought",
                agent_id="alice",
                asset=_ASSET,
                amount_usd=spend,
                price_per_unit_usd=_PRICE,
            )
        ],
        scheduled_asset_sales=[
            ScheduledAssetSale(
                month=sale_month,
                cause_id="sell_vti",
                agent_id="alice",
                asset=_ASSET,
                quantity=_UNITS,
                proceeds_account_id="checking",
                price_per_unit_usd=sale_price,
            )
        ]
        if sale_price is not None
        else [],
        tax_profiles=[
            TaxProfile(
                agent_id="alice",
                filing_status=FilingStatus.SINGLE,
                jurisdiction_ids=["federal_us"],
                tax_authority_agent_id="irs",
            )
        ],
        horizon_months=_HORIZON,
    )


def _lot(scenario: Scenario, *, month: int) -> dict[str, object]:
    lots = simulate(scenario, rollout_count=1, locations={}).asset_lots
    rows = lots.filter((pl.col("lot_id") == "bought") & (pl.col("month_index") == month)).to_dicts()
    return rows[0]


def _cash(scenario: Scenario) -> list[float]:
    run = simulate(scenario, rollout_count=1, locations={})
    return [
        float(v)
        for v in run.cash_balances.filter(pl.col("agent_id") == "alice")
        .sort("month_index")
        .get_column("balance_usd")
        .to_list()
    ]


def _gains(scenario: Scenario) -> dict[str, float]:
    run = simulate(scenario, rollout_count=1, locations={})
    return {
        str(row["classification"]): float(row["gain_usd"])
        for row in run.capital_gains_ytd.filter(pl.col("agent_id") == "alice").to_dicts()
    }


def test_a_purchase_creates_a_lot_at_the_price_its_rollout_paid() -> None:
    """The basic mechanic: $500,000 at $100/unit is 5,000 units with a $100 basis. The basis
    is the assertion that matters — reading the compile-time column would give 0 here."""

    lot = _lot(_scenario(), month=2)

    assert lot["remaining_quantity"] == _UNITS
    assert lot["cost_basis_per_unit_usd"] == _PRICE


def test_the_lot_slot_is_empty_before_its_purchase_month() -> None:
    """The slot is allocated at compile time but must hold nothing until the purchase fires —
    which is also what lets FIFO carry the slot's real purchase month without ordering care:
    a zero-quantity lot contributes nothing to a FIFO walk that reaches it early."""

    assert _lot(_scenario(), month=1)["remaining_quantity"] == 0.0


def test_the_purchase_debits_exactly_what_the_units_cost() -> None:
    """Cash falls by the purchase amount in the purchase month, and by nothing after it."""

    balances = _cash(_scenario())

    assert balances[1] == _OPENING_CASH
    assert balances[2] == _OPENING_CASH - _SPEND
    assert balances[-1] == _OPENING_CASH - _SPEND


def test_cash_is_conserved_across_a_purchase() -> None:
    """Double entry. The cash a purchase spends leaves for the market, which is outside the
    modeled world — so it lands on the `rest_of_world` contra row rather than evaporating.
    Without that credit the ledger would shed $500,000 in month 1."""

    run = simulate(_scenario(), rollout_count=1, locations={})
    state = np.asarray(run.buffers.state.cash_state, dtype=np.int64)
    totals = state.sum(axis=tuple(range(1, state.ndim)))

    assert np.all(totals == totals[0])


def test_an_immediate_resale_at_the_purchase_price_realizes_nothing() -> None:
    """Buying and selling the same units at the same price is a round trip, and must net to
    exactly zero — not to a rounding crumb. Cost and basis-consumption go through the same
    quanta-valuation helper precisely so this holds."""

    assert _gains(_scenario(sale_price=_PRICE, sale_month=2)) == {"stcg": 0.0}


def test_gain_is_measured_against_the_price_the_rollout_paid() -> None:
    """The discriminating test for runtime basis.

    Bought 5,000 units at $100, sold at $150: the gain is $250,000. A basis read from the
    plan's compile-time column would be $0 for a purchased lot and report the full $750,000
    of proceeds as gain — three times the truth, and silently.
    """

    assert _gains(_scenario(sale_price=150.0)) == {"stcg": 250_000.0}


def test_an_underfunded_purchase_buys_what_the_cash_covers() -> None:
    """Buying is discretionary, so a month with less cash than the order buys less rather
    than failing the rollout. The clamp is not silent: the lot records what was actually
    bought, so a caller comparing it against the requested amount sees the shortfall."""

    lot = _lot(_scenario(opening_cash=100_000.0), month=2)

    assert lot["remaining_quantity"] == 1_000.0
    assert lot["cost_basis_per_unit_usd"] == _PRICE
    assert _cash(_scenario(opening_cash=100_000.0))[2] == 0.0


def test_a_purchased_lot_is_markable_even_when_its_execution_price_was_fixed() -> None:
    """A purchase leaves a LOT BEHIND, and that lot must be valuable for the rest of the
    horizon.

    `price_per_unit_usd` fixes the EXECUTION price only. If the scenario's demand for the
    asset's price series were gated on that field — as it is for a sale, which leaves nothing
    behind — the lot's `lot_asset_series_index` would be NO_CODE and it would be unmarkable:
    product summary rejects a holding with no modeled price series, and valuations elsewhere
    read zero. So the demand is unconditional, and this is the test that says so.

    The price doubles after the purchase, so a lot carried at cost rather than at the mark
    fails here too.
    """

    scenario = _scenario().model_copy(
        update={
            "external_series": SeriesModelBundle.independent(
                asset_prices=AssetPriceGroups(security={SecuritySymbol("vti"): Deterministic(levels=_PRICE_PATH)})
            )
        }
    )
    net_worth = [
        float(v)
        for v in monthly_metric_arrays(simulate(scenario, rollout_count=1, locations={}), primary_agent_id="alice")[
            "net_worth_usd"
        ]
    ]

    # Cash is opening minus the spend; the lot is 5,000 units marked at the doubled price.
    assert net_worth[-1] == _OPENING_CASH - _SPEND + _UNITS * 2 * _PRICE


def test_a_purchase_never_starves_an_obligation() -> None:
    """Ordering, stated as behavior: purchases settle AFTER obligations, so an order larger
    than the account can afford alongside its bills cannot manufacture a ruin. The rollout
    survives and simply buys less."""

    run = simulate(_scenario(spend=_OPENING_CASH), rollout_count=1, locations={})

    assert run.rollout_status.get_column("status").to_list() == ["active"]


if __name__ == "__main__":
    pytest_bazel.main()
