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
from finance.augur.rust.differential.fixtures import VTI, checking, taxed
from finance.augur.sim.scenario import InitialLot, OrdinaryIncome, ScheduledAssetSale, ScheduledTransfer
from finance.augur.sim.testing.case import Case, levels, scenario

# One unit bought two years ago for $10,000 and sold for $60,000: a $50,000 long-term gain,
# and no ordinary income anywhere in the scenario.
LOT_BASIS = Decimal(10_000)
SALE_PRICE = Decimal(60_000)
LONG_TERM_GAIN_QUANTA = int((SALE_PRICE - LOT_BASIS) * 100)

# `sim/data/jurisdictions/federal_us.yaml`, single filer, in currency quanta. Both engines
# reach it through the compiled plan, so this is the relation between what the deployment's
# tax law says and what the engines assess — not a second copy handed to one of them.
FEDERAL_STANDARD_DEDUCTION_QUANTA = 1_460_000  # $14,600

# §1211(b): a net capital loss reduces a single filer's ordinary income by at most $3,000,
# and §1212(b) carries the rest forward. Spelled here as the statute's own number rather than
# imported, because neither engine takes it from the scenario — JAX holds it as a default
# argument and Rust reads the fixture field the encoder fills from that same constant — so no
# case can configure it, and only a test stating the figure catches a change to either side
# (issue #5586).
CAPITAL_LOSS_ORDINARY_OFFSET_CAP_QUANTA = 300_000  # $3,000
LOSS_SALE_PRICE = Decimal(1_000)

# The §1(h) stacking case: $30,000 of wages against the same $50,000 gain, chosen so the gain
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
WAGES = Decimal(30_000)
WAGES_QUANTA = int(WAGES * 100)
STACKED_FEDERAL_TAX_QUANTA = 437_225  # $4,372.25


def gain_below_the_deduction_case(*, wages: Decimal = Decimal(0)) -> Case:
    """A long-term gain against a full standard deduction, beside `wages` of ordinary income."""

    return Case(
        scenario=scenario(
            checking(("alice", Decimal(0)), ("irs", Decimal(0)), ("employer", wages)),
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


def realized_loss_case(*, loss: Decimal) -> Case:
    """One long-term lot sold for `loss` less than it cost, and no other income."""

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
                    purchase_month_index=-24,
                    quantity=1.0,
                    cost_basis_per_unit=LOSS_SALE_PRICE + loss,
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
        series={VTI: levels([[LOSS_SALE_PRICE] * 13])},
    )


@pytest.mark.parametrize("backend", BACKENDS, ids=lambda run: run.__name__)
@pytest.mark.parametrize(
    ("loss", "offset_quanta"),
    [(Decimal(2_000), 200_000), (Decimal(30_000), CAPITAL_LOSS_ORDINARY_OFFSET_CAP_QUANTA)],
    ids=["under the cap", "over it"],
)
def test_a_capital_loss_offsets_ordinary_income_only_up_to_the_1211_cap(
    backend: Backend, loss: Decimal, offset_quanta: int
) -> None:
    """The whole loss while it fits under $3,000, and exactly $3,000 once it does not.

    Both cases are needed and neither is redundant: the smaller one shows the offset tracking
    the loss, so the larger one pinning $3,000 is the cap binding rather than a constant that
    happens to be returned whatever the loss.
    """

    breakdown = backend(realized_loss_case(loss=loss)).events.tax_breakdowns
    assert [row["ordinary_income_quanta"] for row in breakdown.to_dicts()] == [-offset_quanta]


@pytest.mark.parametrize("backend", BACKENDS, ids=lambda run: run.__name__)
def test_both_engines_see_wages_beside_the_gain(backend: Backend) -> None:
    """The premise of the test below: a tax figure can be right for an unrelated reason."""

    breakdown = backend(gain_below_the_deduction_case(wages=WAGES)).events.tax_breakdowns
    assert breakdown.height == 1, "one jurisdiction, one tax year"
    row = breakdown.to_dicts()[0]
    assert row["standard_deduction_quanta"] == FEDERAL_STANDARD_DEDUCTION_QUANTA
    assert row["ordinary_income_quanta"] == WAGES_QUANTA
    assert row["ltcg_quanta"] == LONG_TERM_GAIN_QUANTA


@pytest.mark.parametrize("backend", BACKENDS, ids=lambda run: run.__name__)
def test_a_long_term_gain_is_rated_from_where_ordinary_income_leaves_off(backend: Backend) -> None:
    """§1(h): the long-term bracket is walked on total taxable income, not on the gain alone.

    `differential/tax_test.py` already asserts the two engines agree on stacking, and
    `engine/jax_tax_test.py` walks the bracket function directly. Neither reaches the
    composition between them — which taxable income the walk is handed — and that is where
    #5588 was: the walk was right, its input was not, and both engines shared the mistake.

    The deduction case above cannot reach it either, because the whole gain fits inside the 0%
    bracket, where rating it from zero still answers zero. This gain crosses that boundary.
    """

    accruals = backend(gain_below_the_deduction_case(wages=WAGES)).events.tax_accruals
    assert [row["amount_quanta"] for row in accruals.to_dicts()] == [STACKED_FEDERAL_TAX_QUANTA]


if __name__ == "__main__":
    pytest_bazel.main()
