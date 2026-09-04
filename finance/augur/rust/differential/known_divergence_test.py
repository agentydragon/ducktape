"""Divergences the fuzzer found that the engines have not been made to agree on yet.

A burn-down, not an archive: an entry leaves this file when the two engines agree, taking
its fixture with it. Each pins what each engine answers today, so a change to either side
fails here and whoever made it decides which answer was meant — rather than the disagreement
quietly moving to a new number.

The fuzz targets also fail on everything recorded here, and deliberately: nothing below is
excused, canonicalized away, or generated around.
"""

import copy
from typing import Any

import pytest_bazel

from finance.augur.rust.differential.backend import SimulationResult, run_jax, run_rust
from finance.augur.sim.events import EVENT_FRAMES

# The gain is 1,000 quanta long-term, the agent has no ordinary income at all, and the
# standard deduction is a hundred times the gain. `rust/tax.rs` computes the deduction
# against ordinary income only and then stacks the whole preferential gain on top of the
# resulting zero — its comment says this matches the existing simulator, and for every
# hand-written fixture so far it did, because none of them held a long-term gain while
# ordinary income sat under the standard deduction. JAX lets the unused deduction shelter
# the gain, which is also what the US rule it models does.
LONG_TERM_LOT_FIXTURE: dict[str, Any] = {
    "schema_version": 8,
    "currency_code": "USD",
    "currency_quantum": "0.01",
    "rollout_count": 1,
    "scenario": {
        "horizon_months": 12,
        "accounts": [
            {"account": {"agent_id": "trader", "account_id": "checking"}, "opening_balance": 0},
            {"account": {"agent_id": "revenue", "account_id": "checking"}, "opening_balance": 0},
        ],
        "scheduled_transfers": [],
        "recurring_transfers": [],
        "obligations": [],
        "initial_lots": [
            {
                "lot_id": "trader-lot",
                "agent_id": "trader",
                "account_id": "brokerage",
                "asset_id": "vti",
                "purchase_month": -24,
                "quantity_scale": 1_000_000,
                "units": 10_000_000,
                "basis": 1_000,
            }
        ],
        "scheduled_sales": [
            {
                "month": 0,
                "cause_id": "sell-everything",
                "agent_id": "trader",
                "account_id": "brokerage",
                "asset_id": "vti",
                "units": 10_000_000,
                "proceeds_account_id": "checking",
            }
        ],
        "tax_profiles": [
            {
                "agent_id": "trader",
                "tax_authority_agent_id": "revenue",
                "jurisdictions": [
                    {
                        "jurisdiction_id": "federal_us",
                        "ordinary_brackets": [{"upper": None, "rate_ppb": 100_000_000}],
                        "long_term_capital_gain_brackets": [{"upper": None, "rate_ppb": 200_000_000}],
                        "standard_deduction": 100_000,
                        "max_capital_loss_ordinary_offset": 0,
                    }
                ],
            }
        ],
    },
    "series": [{"series_id": "security:vti", "snapshots": 13, "values": [200] * 13}],
}


def _loss_fixture() -> dict[str, Any]:
    """The same shape, sold at a loss, with a §1211 ordinary-offset cap below the loss."""

    fixture = copy.deepcopy(LONG_TERM_LOT_FIXTURE)
    fixture["scenario"]["initial_lots"][0]["basis"] = 300_000
    fixture["scenario"]["tax_profiles"][0]["jurisdictions"][0]["max_capital_loss_ordinary_offset"] = 50_000
    return fixture


def _ordinary_income(result: SimulationResult) -> list[int]:
    return [int(value) for value in result.events.frame(EVENT_FRAMES.tax_breakdowns)["ordinary_income_quanta"]]


def test_jax_caps_the_capital_loss_ordinary_offset_at_its_own_constant() -> None:
    """The configured §1211 cap reaches Rust and not JAX.

    `_net_capital_gains_jnp` takes `max_ordinary_offset_quanta` as a default argument of
    300_000 and no caller passes one, so JAX offsets the whole 298_000 loss while Rust honours
    the 50_000 the fixture asked for. Every hand-written fixture set the cap to exactly
    300_000, which is why the pair looked consistent.
    """

    fixture = _loss_fixture()
    assert _ordinary_income(run_jax(fixture)) == [-298_000]
    assert _ordinary_income(run_rust(fixture)) == [-50_000]


if __name__ == "__main__":
    pytest_bazel.main()
