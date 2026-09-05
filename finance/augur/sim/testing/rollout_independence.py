"""What one rollout's tax liability did is not evidence about another's.

Rollouts are independent sample paths that happen to be computed together, so a reader that
reduces over the rollout axis and reports the answer back across it will attribute one path's
history to all of them. That is not a rounding difference or an off-by-one: it puts a row in a
rollout that nothing happened in. It was a real bug in the JAX reader (#5629), found because
Rust reported the months differently.

The case below is the smallest shape that can show it, and it is worth saying why it takes
this much: a single rollout cannot exhibit it at all, because there is no sibling to leak
from. Two rollouts whose liabilities move in *different* months are the minimum, and getting
them requires a gain large enough to survive the standard deduction in one path and not exist
in the other.

Stated against a `SimulationResult` rather than one engine's output, because "a path reports
only its own months" is a claim about what a simulator is, not about how one computes it.
"""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import polars as pl
import pytest

from finance.augur.model.series import SecurityKey, SecuritySymbol
from finance.augur.sim.scenario import InitialLot, ScheduledAssetSale
from finance.augur.sim.testing.case import Case, scenario
from finance.augur.sim.testing.fixtures import checking, taxed
from finance.augur.sim.testing.simulation_result import Backend

VTI = SecurityKey(symbol=SecuritySymbol("vti"))
HORIZON_MONTHS = 25
SALE_MONTH = 15
BASIS = Decimal(100)
# One path sells at cost and owes nothing; the other sells into a gain far above the standard
# deduction, so only it has a liability to settle — and only in its own months.
FLAT_PRICE = 100.0
SPIKED_PRICE = 20_000.0
QUIET, TAXED = 0, 1
# Each tax year closes at month 11 and 23; the assessment lands the month after, and a
# liability that is owed is settled the month after that.
ASSESSED_YEAR_ONE, ASSESSED_YEAR_TWO, SETTLED_YEAR_TWO = 12, 24, 25


def one_path_owes_and_one_does_not() -> Case:
    """Two paths over one scenario: the price spikes in one of them and not the other."""

    prices = np.full((2, HORIZON_MONTHS + 1), FLAT_PRICE)
    prices[TAXED, :] = SPIKED_PRICE
    return Case(
        scenario=scenario(
            checking(("alice", Decimal(0)), ("irs", Decimal(0))),
            horizon_months=HORIZON_MONTHS,
            initial_lots=[
                InitialLot(
                    lot_id="alice-vti",
                    agent_id="alice",
                    account_id="checking",
                    asset=VTI,
                    purchase_month_index=-24,  # comfortably long-term
                    quantity=10.0,
                    cost_basis_per_unit=BASIS,
                )
            ],
            scheduled_asset_sales=[
                ScheduledAssetSale(
                    month=SALE_MONTH,
                    cause_id="sell-vti",
                    agent_id="alice",
                    source_account_id="checking",
                    asset=VTI,
                    quantity=10.0,
                    proceeds_account_id="checking",
                )
            ],
            tax_profiles=[taxed("alice", "federal_us")],
        ),
        rollout_count=2,
        series={VTI: prices},
    )


class RolloutIndependenceAcceptance:
    """Inherit and supply `backend`. Add nothing unless the engine owes something extra."""

    @pytest.fixture
    def backend(self) -> Backend:
        raise NotImplementedError("an acceptance module names the engine it runs")

    def test_only_one_rollout_owes_anything(self, backend: Backend) -> None:
        """The premise. Without it the assertion below could pass on a case that proves nothing."""

        liabilities = backend(one_path_owes_and_one_does_not()).tax_liabilities
        peaks = dict(
            liabilities.group_by("rollout_index").agg(pl.col("amount_owed_quanta").max().alias("peak")).iter_rows()
        )
        assert peaks[QUIET] == 0, "the flat path should never owe tax"
        assert peaks[TAXED] > 0, "the spiked path should owe tax on its gain"

    def test_a_rollout_reports_only_the_months_its_own_liability_moved(self, backend: Backend) -> None:
        """A settlement in one path must not put a row in another.

        The paths are independent; they share only the batch they are computed in. Each tax
        year closes at month 11 and 23 and is assessed the month after, so both paths report at
        12 and 24. Only the taxed path owes anything, so only it settles — at 25. A reader that
        asks whether a liability moved in ANY rollout, and answers for all of them, gives the
        quiet path a row at 25 as well, reporting a settlement in a path that had nothing to
        settle.
        """

        rows = backend(one_path_owes_and_one_does_not()).tax_liabilities

        def months(rollout: int) -> list[int]:
            return sorted(rows.filter(pl.col("rollout_index") == rollout).get_column("month_index").to_list())

        assert months(TAXED) == [ASSESSED_YEAR_ONE, ASSESSED_YEAR_TWO, SETTLED_YEAR_TWO]
        assert months(QUIET) == [ASSESSED_YEAR_ONE, ASSESSED_YEAR_TWO]
