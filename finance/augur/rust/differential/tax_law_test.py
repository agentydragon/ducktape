"""What the tax code says, asserted against both engines.

Every other suite here asks whether the two engines agree. That cannot catch a rule both
implement the same way and both get wrong, which is exactly what happened with the case
below: JAX and Rust compute it identically, so 30 fixtures and a randomized campaign of 320
compared cases all passed while both answers were wrong.

So this suite states the answer the statute gives and points it at both engines. A test here
failing on both backends is the useful outcome, not a contradiction.

Both engines must be reading the same tax law for such a test to mean anything, and today
that is not automatic: `fixture_adapter.py` passes only `jurisdiction_ids` to JAX, which
then loads `sim/data/jurisdictions/*.yaml` itself, while Rust reads the fixture. The
schedule below is therefore transcribed from `federal_us.yaml` exactly, and
`test_both_engines_are_reading_the_same_schedule` fails if that stops being true.
"""

from typing import Any

import pytest
import pytest_bazel

from finance.augur.rust.differential.backend import BACKENDS
from finance.augur.rust.fixture_spec import account_ref, fixture, shared_series

# `sim/data/jurisdictions/federal_us.yaml`, single filer, in currency quanta. Transcribed
# rather than imported: Rust must be handed the same numbers JAX loads for itself, and a
# drift between the two is what the schedule test below is for.
FEDERAL_ORDINARY_BRACKETS = [
    {"upper": 1_160_000, "rate_ppb": 100_000_000},
    {"upper": 4_715_000, "rate_ppb": 120_000_000},
    {"upper": 10_052_500, "rate_ppb": 220_000_000},
    {"upper": 19_195_000, "rate_ppb": 240_000_000},
    {"upper": 24_372_500, "rate_ppb": 320_000_000},
    {"upper": 60_935_000, "rate_ppb": 350_000_000},
    {"upper": None, "rate_ppb": 370_000_000},
]
FEDERAL_LTCG_BRACKETS = [
    {"upper": 4_702_500, "rate_ppb": 0},
    {"upper": 51_890_000, "rate_ppb": 150_000_000},
    {"upper": None, "rate_ppb": 200_000_000},
]
FEDERAL_STANDARD_DEDUCTION = 1_460_000  # $14,600

# One unit bought two years ago for $10,000 and sold for $60,000: a $50,000 long-term gain,
# and no ordinary income anywhere in the scenario.
LOT_BASIS_QUANTA = 1_000_000
SALE_PRICE_QUANTA = 6_000_000
LONG_TERM_GAIN_QUANTA = SALE_PRICE_QUANTA - LOT_BASIS_QUANTA


def gain_below_the_deduction_fixture() -> dict[str, Any]:
    """A long-term gain, no ordinary income, and a standard deduction larger than zero."""

    scenario = {
        "horizon_months": 12,
        "accounts": [
            {"account": account_ref("alice", "checking"), "opening_balance": 0},
            {"account": account_ref("irs", "checking"), "opening_balance": 0},
        ],
        "scheduled_transfers": [],
        "recurring_transfers": [],
        "obligations": [],
        "recurring_obligations": [],
        "initial_lots": [
            {
                "lot_id": "alice-vti",
                "agent_id": "alice",
                "account_id": "checking",
                "asset_id": "vti",
                "purchase_month": -24,  # comfortably long-term
                "quantity_scale": 1_000_000,
                "units": 1_000_000,
                "basis": LOT_BASIS_QUANTA,
            }
        ],
        "scheduled_sales": [
            {
                "month": 0,
                "cause_id": "sell-vti",
                "agent_id": "alice",
                "account_id": "checking",
                "asset_id": "vti",
                "units": 1_000_000,
                "proceeds_account_id": "checking",
            }
        ],
        "tax_profiles": [
            {
                "agent_id": "alice",
                "tax_authority_agent_id": "irs",
                "jurisdictions": [
                    {
                        "jurisdiction_id": "federal_us",
                        "ordinary_brackets": FEDERAL_ORDINARY_BRACKETS,
                        "long_term_capital_gain_brackets": FEDERAL_LTCG_BRACKETS,
                        "standard_deduction": FEDERAL_STANDARD_DEDUCTION,
                        "max_capital_loss_ordinary_offset": 300_000,
                    }
                ],
            }
        ],
    }
    series = [shared_series("security:vti", rollout_count=1, path=[SALE_PRICE_QUANTA] * 13)]
    return fixture(scenario, series, rollout_count=1)


@pytest.mark.parametrize("backend", BACKENDS, ids=lambda run: run.__name__)
def test_both_engines_are_reading_the_same_schedule(backend) -> None:
    """The premise of the test below: neither engine is quietly on a different schedule.

    JAX loads its own YAML and Rust reads the fixture, so a test that asserted a tax amount
    without checking this could pass or fail for a reason that has nothing to do with the
    rule under test.
    """

    breakdown = backend(gain_below_the_deduction_fixture()).events.tax_breakdowns
    assert breakdown.height == 1, "one jurisdiction, one tax year"
    row = breakdown.to_dicts()[0]
    assert row["standard_deduction_quanta"] == FEDERAL_STANDARD_DEDUCTION
    assert row["ltcg_quanta"] == LONG_TERM_GAIN_QUANTA
    assert row["ordinary_income_quanta"] == 0


@pytest.mark.parametrize("backend", BACKENDS, ids=lambda run: run.__name__)
def test_an_unused_standard_deduction_shelters_a_long_term_gain(backend) -> None:
    """§63 nets the deduction against taxable income; §1(h) then rates what is left.

    Taxable income is $50,000 of gain less the $14,600 deduction, so $35,400 — all of it
    net capital gain, and below the $47,025 top of the 0% bracket. The tax is zero.

    The IRS Qualified Dividends and Capital Gain Tax Worksheet makes the ordering explicit:
    it opens at Form 1040 line 15, which is taxable income *after* the deduction. An engine
    that floors ordinary taxable income at zero and then rates the whole gain stacked on top
    is throwing the unused deduction away, and charges $446.25 on a return that owes nothing.
    """

    accruals = backend(gain_below_the_deduction_fixture()).events.tax_accruals
    assert [row["amount_quanta"] for row in accruals.to_dicts()] == [0]


if __name__ == "__main__":
    pytest_bazel.main()
