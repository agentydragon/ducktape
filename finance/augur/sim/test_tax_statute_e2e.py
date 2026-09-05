"""What the tax code says, asserted against a whole simulation.

The rest of the tax coverage here calls `_apply_brackets`, `_apply_ltcg_brackets` and
`_net_capital_gains_jnp` directly (`engine/jax_tax_test.py`). Those are the bracket walks;
the rule below lives above them, in how the year-end assessment splits taxable income before
either walk runs, so nothing that tests a walk in isolation can reach it.

That mattered: both this engine and the Rust one floored ordinary taxable income at zero and
then rated the whole long-term gain stacked on top, throwing an unused standard deduction
away. Thirty hand-written differential fixtures and a randomized campaign all passed, because
the two engines were wrong in the same way. Only a test that states what the statute answers
catches that, so this file states it and runs a scenario end to end to get there.

Written against `Scenario` and `simulate` rather than engine internals so the same cases can
be pointed at a second engine when there is one.
"""

from __future__ import annotations

from decimal import Decimal

import polars as pl
import pytest_bazel

from finance.augur.model.deterministic import Deterministic
from finance.augur.model.level_series_groups import AssetPriceGroups
from finance.augur.model.series import SecurityKey, SecuritySymbol
from finance.augur.model.series_model import SeriesModelBundle
from finance.augur.sim.scenario import (
    Agent,
    InitialAccountBalance,
    InitialLot,
    OrdinaryIncome,
    Scenario,
    ScheduledAssetSale,
    ScheduledTransfer,
    TaxProfile,
)
from finance.augur.sim.simulate import simulate

_VTI = SecurityKey(symbol=SecuritySymbol("vti"))

# One unit bought two years ago for $10,000 and sold for $60,000: a $50,000 long-term gain,
# and no ordinary income anywhere in the scenario.
_LOT_BASIS = Decimal(10_000)
_SALE_PRICE = Decimal(60_000)
_LONG_TERM_GAIN_QUANTA = int((_SALE_PRICE - _LOT_BASIS) * 100)

# `sim/data/jurisdictions/federal_us.yaml`, single filer, in currency quanta. The engine reads
# it through the compiled plan, so this is the relation between what the deployment's tax law
# says and what the engine assesses — not a second copy handed to it.
_FEDERAL_STANDARD_DEDUCTION_QUANTA = 1_460_000  # $14,600

_HORIZON_MONTHS = 12

# The §1(h) stacking case. $30,000 of wages against the same $50,000 gain, chosen so the gain
# straddles the top of the 0% long-term bracket instead of sitting wholly inside it.
#
#   ordinary taxable   $30,000 - $14,600 deduction      = $15,400
#   ordinary tax       $11,600 @ 10% + $3,800 @ 12%     =  $1,616.00
#   gain at 0%         $47,025 bracket top - $15,400    = $31,625
#   gain at 15%        $50,000 - $31,625 = $18,375      =  $2,756.25
#                                                          ---------
#                                                          $4,372.25
#
# Every figure is from `sim/data/jurisdictions/federal_us.yaml`, single filer. An engine that
# rates the gain from zero rather than from where ordinary taxable income leaves off puts
# $47,025 in the 0% band and only $2,975 at 15%, and assesses $2,062.25.
_WAGES = Decimal(30_000)
_WAGES_QUANTA = int(_WAGES * 100)
_STACKED_FEDERAL_TAX_QUANTA = 437_225  # $4,372.25


def _sale_scenario(*, cost_basis_per_unit: Decimal, wages: Decimal = Decimal(0)) -> Scenario:
    """One long-term lot sold at `_SALE_PRICE`, and `wages` of ordinary income if any."""

    return Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="irs"), Agent(agent_id="employer")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance=Decimal(0)),
            InitialAccountBalance(agent_id="irs", account_id="checking", balance=Decimal(0)),
            InitialAccountBalance(agent_id="employer", account_id="checking", balance=wages),
        ],
        scheduled_transfers=[
            ScheduledTransfer(
                month=0,
                cause_id="wages",
                from_agent_id="employer",
                from_account_id="checking",
                to_agent_id="alice",
                to_account_id="checking",
                amount=wages,
                income_category=OrdinaryIncome(),
            )
        ]
        if wages
        else [],
        initial_lots=[
            InitialLot(
                lot_id="alice-vti",
                agent_id="alice",
                account_id="checking",
                asset=_VTI,
                purchase_month_index=-24,  # comfortably long-term
                quantity=1.0,
                cost_basis_per_unit=cost_basis_per_unit,
            )
        ],
        scheduled_asset_sales=[
            ScheduledAssetSale(
                month=0,
                cause_id="sell-vti",
                agent_id="alice",
                source_account_id="checking",
                asset=_VTI,
                quantity=1.0,
                proceeds_account_id="checking",
            )
        ],
        tax_profiles=[TaxProfile(agent_id="alice", jurisdiction_ids=["federal_us"], tax_authority_agent_id="irs")],
        external_series=SeriesModelBundle.independent(
            asset_prices=AssetPriceGroups(
                security={SecuritySymbol("vti"): Deterministic(levels=[float(_SALE_PRICE)] * (_HORIZON_MONTHS + 1))}
            )
        ),
        horizon_months=_HORIZON_MONTHS,
    )


def _run(scenario: Scenario) -> tuple[pl.DataFrame, pl.DataFrame]:
    run = simulate(scenario, rollout_count=1, locations={})
    return run.events_log.tax_accruals, run.events_log.tax_breakdowns


def test_the_case_really_is_a_bare_gain_against_a_full_deduction() -> None:
    """The premise of the test below.

    Without this, the tax amount could be right or wrong for a reason having nothing to do
    with the rule under test — a gain that came out short-term, say, or ordinary income
    leaking in from somewhere.
    """

    _, breakdowns = _run(_sale_scenario(cost_basis_per_unit=_LOT_BASIS))
    assert breakdowns.height == 1, "one jurisdiction, one tax year"
    row = breakdowns.to_dicts()[0]
    assert row["standard_deduction_quanta"] == _FEDERAL_STANDARD_DEDUCTION_QUANTA
    assert row["ltcg_quanta"] == _LONG_TERM_GAIN_QUANTA
    assert row["ordinary_income_quanta"] == 0


def test_an_unused_standard_deduction_shelters_a_long_term_gain() -> None:
    """§63 nets the deduction against taxable income; §1(h) then rates what is left.

    Taxable income is $50,000 of gain less the $14,600 deduction, so $35,400 — all of it net
    capital gain, and below the $47,025 top of the 0% bracket. The tax is zero.

    The IRS Qualified Dividends and Capital Gain Tax Worksheet makes the ordering explicit: it
    opens at Form 1040 line 15, which is taxable income *after* the deduction. An engine that
    floors ordinary taxable income at zero and rates the whole gain stacked on top is throwing
    the unused deduction away, and charges $446.25 on a return that owes nothing.
    """

    accruals, _ = _run(_sale_scenario(cost_basis_per_unit=_LOT_BASIS))
    assert [row["amount_quanta"] for row in accruals.to_dicts()] == [0]


def test_the_stacking_case_really_is_wages_beside_a_gain() -> None:
    """The premise. A tax figure can come out right for reasons unrelated to the rule."""

    _, breakdowns = _run(_sale_scenario(cost_basis_per_unit=_LOT_BASIS, wages=_WAGES))
    assert breakdowns.height == 1, "one jurisdiction, one tax year"
    row = breakdowns.to_dicts()[0]
    assert row["standard_deduction_quanta"] == _FEDERAL_STANDARD_DEDUCTION_QUANTA
    assert row["ordinary_income_quanta"] == _WAGES_QUANTA
    assert row["ltcg_quanta"] == _LONG_TERM_GAIN_QUANTA


def test_a_long_term_gain_is_rated_from_where_ordinary_income_leaves_off() -> None:
    """§1(h): the long-term bracket is walked on total taxable income, not on the gain alone.

    The gain here straddles the top of the 0% bracket, which the deduction case above cannot
    reach — there the whole gain fits inside that bracket, so an engine that rated it from zero
    would still answer zero. Only a gain that crosses the boundary tells the two apart, and it
    is the same composition #5588 got wrong: what the engine hands the long-term bracket walk,
    rather than the walk itself.
    """

    accruals, _ = _run(_sale_scenario(cost_basis_per_unit=_LOT_BASIS, wages=_WAGES))
    assert [row["amount_quanta"] for row in accruals.to_dicts()] == [_STACKED_FEDERAL_TAX_QUANTA]


if __name__ == "__main__":
    pytest_bazel.main()
