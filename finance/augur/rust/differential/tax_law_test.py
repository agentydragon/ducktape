"""What the tax code says, asserted against both engines.

Every other suite here asks whether the two engines agree. That cannot catch a rule both
implement the same way and both get wrong, which is exactly what happened with the case
below: JAX and Rust compute it identically, so 30 cases and a randomized campaign of 320
compared ones all passed while both answers were wrong.

So this suite states the answer the statute gives and points it at both engines. A test here
failing on both backends is the useful outcome, not a contradiction.
"""

from decimal import Decimal

import pytest
import pytest_bazel

from finance.augur.rust.differential.backend import BACKENDS, Backend
from finance.augur.rust.differential.case import Case, levels, scenario
from finance.augur.rust.differential.fixtures import VTI, checking, taxed
from finance.augur.sim.scenario import InitialLot, ScheduledAssetSale

# One unit bought two years ago for $10,000 and sold for $60,000: a $50,000 long-term gain,
# and no ordinary income anywhere in the scenario.
LOT_BASIS = Decimal(10_000)
SALE_PRICE = Decimal(60_000)
LONG_TERM_GAIN_QUANTA = int((SALE_PRICE - LOT_BASIS) * 100)

# `sim/data/jurisdictions/federal_us.yaml`, single filer, in currency quanta. Both engines
# reach it through the compiled plan, so this is the relation between what the deployment's
# tax law says and what the engines assess — not a second copy handed to one of them.
FEDERAL_STANDARD_DEDUCTION_QUANTA = 1_460_000  # $14,600


def gain_below_the_deduction_case() -> Case:
    """A long-term gain, no ordinary income, and a standard deduction larger than zero."""

    return Case(
        scenario=scenario(
            checking(("alice", Decimal(0)), ("irs", Decimal(0))),
            horizon_months=12,
            initial_lots=[
                InitialLot(
                    lot_id="alice-vti",
                    agent_id="alice",
                    account_id="checking",
                    asset=VTI,
                    purchase_month_index=-24,  # comfortably long-term
                    quantity=1.0,
                    cost_basis_per_unit=LOT_BASIS,
                )
            ],
            scheduled_asset_sales=[
                ScheduledAssetSale(
                    month=0,
                    cause_id="sell-vti",
                    agent_id="alice",
                    source_account_id="checking",
                    asset=VTI,
                    quantity=1.0,
                    proceeds_account_id="checking",
                )
            ],
            tax_profiles=[taxed("alice", "federal_us")],
        ),
        rollout_count=1,
        series={VTI: levels([[SALE_PRICE] * 13])},
    )


@pytest.mark.parametrize("backend", BACKENDS, ids=lambda run: run.__name__)
def test_both_engines_assess_the_deduction_and_gain_the_statute_names(backend: Backend) -> None:
    """The premise of the test below: the case really is a bare gain against a full deduction.

    Without this, a tax amount could be right or wrong for a reason that has nothing to do
    with the rule under test.
    """

    breakdown = backend(gain_below_the_deduction_case()).events.tax_breakdowns
    assert breakdown.height == 1, "one jurisdiction, one tax year"
    row = breakdown.to_dicts()[0]
    assert row["standard_deduction_quanta"] == FEDERAL_STANDARD_DEDUCTION_QUANTA
    assert row["ltcg_quanta"] == LONG_TERM_GAIN_QUANTA
    assert row["ordinary_income_quanta"] == 0


@pytest.mark.parametrize("backend", BACKENDS, ids=lambda run: run.__name__)
def test_an_unused_standard_deduction_shelters_a_long_term_gain(backend: Backend) -> None:
    """§63 nets the deduction against taxable income; §1(h) then rates what is left.

    Taxable income is $50,000 of gain less the $14,600 deduction, so $35,400 — all of it
    net capital gain, and below the $47,025 top of the 0% bracket. The tax is zero.

    The IRS Qualified Dividends and Capital Gain Tax Worksheet makes the ordering explicit:
    it opens at Form 1040 line 15, which is taxable income *after* the deduction. An engine
    that floors ordinary taxable income at zero and then rates the whole gain stacked on top
    is throwing the unused deduction away, and charges $446.25 on a return that owes nothing.
    """

    accruals = backend(gain_below_the_deduction_case()).events.tax_accruals
    assert [row["amount_quanta"] for row in accruals.to_dicts()] == [0]


if __name__ == "__main__":
    pytest_bazel.main()
