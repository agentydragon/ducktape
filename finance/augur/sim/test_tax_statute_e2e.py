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
    Scenario,
    ScheduledAssetSale,
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


def _sale_scenario(*, cost_basis_per_unit: Decimal) -> Scenario:
    """One long-term lot sold at `_SALE_PRICE`, and no other income at all."""

    return Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="irs")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance=Decimal(0)),
            InitialAccountBalance(agent_id="irs", account_id="checking", balance=Decimal(0)),
        ],
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


if __name__ == "__main__":
    pytest_bazel.main()
