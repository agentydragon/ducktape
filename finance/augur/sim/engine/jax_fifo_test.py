"""`_fifo_sell` is the engine's only disposal executor, and it executes rather than decides.

The e2e tests prove today's SCENARIOS sell the right lots. What they cannot prove is the
property that lets one function serve every caller: it moves exactly the quanta it was
handed, and refuses outright when it cannot. Converting a dollar amount into a quantity is
the policy's job — see `target_allocation._quanta_for_cents` and its tests — because doing
it here would be the engine choosing how much to trade.
"""

from __future__ import annotations

import jax.numpy as jnp
import numpy as np
import pytest_bazel

from finance.augur.sim.engine.jax_engine import _fifo_sell

# Three lots of one asset, one rollout. A pool is (agent, account, asset), so every lot in it
# shares a price and a quantity scale.
_ORDERED = np.asarray([0, 1, 2], dtype=np.int64)


def _sell(*, quanta: list[int], target: int, price_cents: int = 100, scale: int = 1, basis_cents: int = 0):
    lots = len(quanta)
    return _fifo_sell(
        jnp.asarray([quanta], dtype=jnp.int64),
        _ORDERED[:lots],
        jnp.asarray([target], dtype=jnp.int64),
        jnp.asarray([price_cents], dtype=jnp.int64),
        jnp.asarray([[basis_cents]] * lots, dtype=jnp.int64),
        jnp.asarray([scale] * lots, dtype=jnp.int64),
    )


def test_it_sells_exactly_what_it_was_asked_for_oldest_lot_first() -> None:
    """Exactly, not approximately. The order is the decision; an executor that rounded it would
    be making a different trade than the one the policy chose."""

    sold, _proceeds, _basis = _sell(quanta=[100, 100, 100], target=250)

    assert sold.tolist() == [[100, 100, 50]]


def test_it_refuses_an_oversell_rather_than_part_filling_it() -> None:
    """A part fill would leave the obligation short anyway, but silently. Refusing sends the
    rollout to settlement, where an unpaid bill is visible as a failure."""

    sold, _proceeds, _basis = _sell(quanta=[10, 10], target=999)

    assert sold.tolist() == [[0, 0]]


def test_proceeds_and_basis_are_valued_on_what_actually_sold() -> None:
    """The cash leg is derived, never decided: quanta actually moved, times this month's price.
    Basis comes off the same quanta, so an immediate resale of a whole lot nets zero gain."""

    sold, proceeds, basis = _sell(quanta=[10, 10], target=15, price_cents=250, basis_cents=100)

    assert sold.tolist() == [[10, 5]]
    assert proceeds.tolist() == [[2_500, 1_250]]
    assert basis.tolist() == [[1_000, 500]]


def test_a_fractional_scale_sells_fractions_of_a_unit() -> None:
    """Quanta, not units — crypto is held in satoshis. At scale 100 a target of 250 quanta is
    2.5 units, and valuing it has to divide by the scale rather than treat quanta as units."""

    sold, proceeds, _basis = _sell(quanta=[400], target=250, price_cents=1_000, scale=100)

    assert sold.tolist() == [[250]]
    assert proceeds.tolist() == [[2_500]]


def test_a_zero_target_touches_nothing() -> None:
    """The common case: the phase runs every month for every policy, and a month inside the
    band orders nothing. Ordering zero has to be inert, not merely cheap."""

    sold, proceeds, basis = _sell(quanta=[10, 10], target=0)

    assert sold.tolist() == [[0, 0]]
    assert proceeds.tolist() == [[0, 0]]
    assert basis.tolist() == [[0, 0]]


if __name__ == "__main__":
    pytest_bazel.main()
