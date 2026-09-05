"""What one rollout's tax liability did is not evidence about another's.

Rollouts are independent sample paths that happen to be computed in one batched array, so a
reader that reduces over the rollout axis and reports the answer back across it will attribute
one path's history to all of them. That is not a rounding difference or an off-by-one: it puts
a row in a rollout that nothing happened in.

The case below is the smallest shape that can show it, and it is worth saying why it takes
this much: a single rollout cannot exhibit the bug at all, because there is no sibling to leak
from. Two rollouts whose liabilities move in *different* months are the minimum, and getting
them requires a gain large enough to survive the standard deduction in one path and not exist
in the other.
"""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import polars as pl
import pytest_bazel

from finance.augur.model.series import SecurityKey, SecuritySymbol
from finance.augur.sim.codec.plan import SimulationRun
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.scenario import (
    Agent,
    InitialAccountBalance,
    InitialLot,
    Scenario,
    ScheduledAssetSale,
    TaxProfile,
)
from finance.augur.sim.simulate import simulate_with_external_series
from finance.augur.sim.testing.state_helpers import tax_liabilities

_VTI = SecurityKey(symbol=SecuritySymbol("vti"))
_HORIZON_MONTHS = 25
_SALE_MONTH = 15
_BASIS = Decimal(100)
# One path sells at cost and owes nothing; the other sells into a gain far above the standard
# deduction, so only it has a liability to settle — and only in its own months.
_FLAT_PRICE = 100.0
_SPIKED_PRICE = 20_000.0
_QUIET, _TAXED = 0, 1
# Each tax year closes at month 11 and 23; the assessment lands the month after, and a
# liability that is owed is settled the month after that.
_ASSESSED_YEAR_ONE, _ASSESSED_YEAR_TWO, _SETTLED_YEAR_TWO = 12, 24, 25


def _run() -> SimulationRun:
    scenario = Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="irs")],
        initial_cash=[
            InitialAccountBalance(agent_id=agent_id, account_id="checking", balance=Decimal(0))
            for agent_id in ("alice", "irs")
        ],
        initial_lots=[
            InitialLot(
                lot_id="alice-vti",
                agent_id="alice",
                account_id="checking",
                asset=_VTI,
                purchase_month_index=-24,  # comfortably long-term
                quantity=10.0,
                cost_basis_per_unit=_BASIS,
            )
        ],
        scheduled_asset_sales=[
            ScheduledAssetSale(
                month=_SALE_MONTH,
                cause_id="sell-vti",
                agent_id="alice",
                source_account_id="checking",
                asset=_VTI,
                quantity=10.0,
                proceeds_account_id="checking",
            )
        ],
        tax_profiles=[TaxProfile(agent_id="alice", jurisdiction_ids=["federal_us"], tax_authority_agent_id="irs")],
        horizon_months=_HORIZON_MONTHS,
    )
    prices = np.full((2, _HORIZON_MONTHS + 1), _FLAT_PRICE)
    prices[_TAXED, :] = _SPIKED_PRICE
    return simulate_with_external_series(
        scenario,
        rollout_count=2,
        external_series=ExternalSeriesContext.from_level_blocks(
            [(_VTI, prices)], rollout_count=2, horizon_months=_HORIZON_MONTHS
        ),
        locations={},
    )


def test_only_one_rollout_owes_anything() -> None:
    """The premise. Without it the assertion below could pass on a case that proves nothing."""

    owed = tax_liabilities(_run()).group_by("rollout_index").agg(pl.col("amount_owed_quanta").max().alias("peak"))
    peaks = dict(owed.iter_rows())
    assert peaks[_QUIET] == 0, "the flat path should never owe tax"
    assert peaks[_TAXED] > 0, "the spiked path should owe tax on its gain"


def test_a_rollout_reports_only_the_months_its_own_liability_moved() -> None:
    """A settlement in one path must not put a row in another.

    The paths are independent; they share only the array they are computed in. Each tax year
    closes at month 11 and 23 and is assessed the month after, so both paths report at 12 and
    24. Only the taxed path owes anything, so only it settles — at 25. A reader that asks
    whether a liability moved in ANY rollout, and answers for all of them, gives the quiet path
    a row at 25 as well, reporting a settlement in a path that had nothing to settle.
    """

    rows = tax_liabilities(_run())

    def months(rollout: int) -> list[int]:
        return sorted(rows.filter(pl.col("rollout_index") == rollout).get_column("month_index").to_list())

    assert months(_TAXED) == [_ASSESSED_YEAR_ONE, _ASSESSED_YEAR_TWO, _SETTLED_YEAR_TWO]
    assert months(_QUIET) == [_ASSESSED_YEAR_ONE, _ASSESSED_YEAR_TWO]


if __name__ == "__main__":
    pytest_bazel.main()
