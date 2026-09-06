"""What the target-allocation policy does that only the JAX engine can be asked about.

The policy's behaviour through an engine is `sim/testing/target_allocation.py`, asserted
against both engines. What is left here are two things that are not claims about a
simulator at all:

- **Cash conservation.** Stated over JAX's cash tensor including its external contra row,
  which is how the JAX engine keeps a flow to an unmodeled counterparty from vanishing. It
  is the only property that sees a disposal crediting proceeds with no matching debit — net
  worth stays correct when a lot leaves as its cash arrives. Rust has no counterpart row;
  its double-entry ledger validates the same thing on every journal entry.
- **What is in the compiled program.** A sleeve weight must be traced rather than static, or
  a sweep pays a full XLA compile per point. That is a fact about JAX's cache.

`target_allocation_test.py` proves the policy's arithmetic against its own inputs.
"""

from __future__ import annotations

import numpy as np
import pytest
import pytest_bazel

from finance.augur.sim.codec.plan import SimulationRun
from finance.augur.sim.engine.jax_engine import _program_impl, run_jax_scan
from finance.augur.sim.testing.case import Case
from finance.augur.sim.testing.target_allocation import cash_band_case, cash_band_scenario


def _run(case: Case) -> SimulationRun:
    return SimulationRun(plan=case.plan, output=run_jax_scan(case.plan), external_series=case.external_series)


def _cash_totals(case: Case) -> np.ndarray:
    """The sum over every cash row, including the external one that makes it balance."""

    state = np.asarray(_run(case).output.state.cash, dtype=np.int64)
    return np.asarray(state.sum(axis=tuple(range(1, state.ndim))), dtype=np.int64)


def test_a_sale_the_sleeves_cannot_cover_does_not_mint_money() -> None:
    """Asking for more than the portfolio holds drains it and stops, and the cash tensor still
    conserves — which is what catches a disposal crediting proceeds with no matching debit."""

    totals = _cash_totals(cash_band_case(opening_cash=0, floor=1_000, ceiling=10_000_000))

    assert np.all(totals == totals[0])


def test_a_purchase_does_not_mint_or_burn_money() -> None:
    """The cash leg, checked the only way that catches a missing one. Net worth would look
    right either way — a lot arrives as its cash leaves — so a purchase that debited nobody
    would be invisible to every other assertion about this policy."""

    totals = _cash_totals(cash_band_case(opening_cash=100_000, floor=10_000, ceiling=20_000, purchase_slots=1))

    assert np.all(totals == totals[0])


def test_a_rebalance_does_not_mint_or_burn_money() -> None:
    """Two legs, two counterparties, one conserved tensor. A rebalance is the first month in
    which the agent both sells and buys, so it is the first chance for the sell leg's credit
    and the buy leg's debit to disagree."""

    totals = _cash_totals(
        cash_band_case(opening_cash=50_000, floor=10_000, ceiling=90_000, purchase_slots=1, rebalance_tolerance=0.25)
    )

    assert np.all(totals == totals[0])


def test_rebalancing_without_somewhere_to_buy_is_rejected() -> None:
    """Config-time, because the alternative is a policy that only ever sells: every trigger
    would move the overweight sleeve into cash with no leg to put it back, draining the
    portfolio a little more each time it fires."""

    with pytest.raises(ValueError, match="no purchase slots"):
        cash_band_scenario(opening_cash=50_000, floor=10_000, ceiling=90_000, rebalance_tolerance=0.25)


def test_sweeping_sleeve_weights_does_not_recompile() -> None:
    """A sleeve weight is swept numeric config, so it must be TRACED, not part of the static key.

    It used to be folded into `_Static` through `_FoldedSleeve.weight`, which made every
    distinct weight vector its own XLA program: an eleven-point allocation sweep paid eleven
    full compiles, minutes apiece at a realistic path count, and that is what made a 2000-path
    sweep unrunnable. Nothing about a weight is a shape — only ratios matter, and the
    water-fill divides by `sum(weight)` at runtime.

    Asserted on JAX's own compile cache rather than wall time, which would be flaky.
    """

    _run(cash_band_case(opening_cash=50_000, floor=10_000, ceiling=90_000, weights=(1, 1)))
    warmed = _program_impl._cache_size()

    for weights in ((3, 7), (19, 81), (50, 50)):
        _run(cash_band_case(opening_cash=50_000, floor=10_000, ceiling=90_000, weights=weights))

    assert _program_impl._cache_size() == warmed, (
        "changing sleeve weights added a compiled variant, so weights are back in the static key"
    )


if __name__ == "__main__":
    pytest_bazel.main()
