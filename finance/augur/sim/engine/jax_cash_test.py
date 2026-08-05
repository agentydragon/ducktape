"""`_move_cash` is the engine's only way to move money, so its own properties are the ones
nothing else can check.

The conservation e2e proves the SCENARIOS that exist today conserve cash. It cannot prove the
primitive conserves cash for inputs no scenario produces — in particular an unresolved
counterparty (`-1`), which `AccountSlots.resolve` currently makes unreachable by settling
unknown pairs against `external` at compile time. That reachability is a property of today's
compiler, not of the engine, so the engine's own behaviour on a `-1` gets pinned here.
"""

from __future__ import annotations

import jax
import jax.numpy as jnp
import numpy as np
import pytest_bazel

from finance.augur.sim.engine.jax_engine import _move_cash, _scatter_rows

_ROWS, _R = 4, 3
_WORLD = 3


def _cash(*rows: list[int]) -> jnp.ndarray:
    return jnp.asarray(rows, dtype=jnp.int64)


def _empty() -> jnp.ndarray:
    return jnp.zeros((_ROWS, _R), dtype=jnp.int64)


def test_a_move_debits_one_row_and_credits_another() -> None:
    """The basic mechanic, per rollout: what leaves row 0 arrives in row 1, and the rollout axis
    is independent."""

    moved = _move_cash(_empty(), debit=0, credit=1, amount=jnp.asarray([10, 20, 30]), row_of_world=_WORLD)

    assert moved.tolist() == [[-10, -20, -30], [10, 20, 30], [0, 0, 0], [0, 0, 0]]


def test_every_move_conserves_the_total() -> None:
    """The invariant the whole primitive exists for. Summed over all rows — the agents' plus the
    external one — a move cannot change the total, whatever the rows and amounts are."""

    start = _cash([100, 200, 300], [0, 0, 0], [50, 50, 50], [0, 0, 0])
    moved = _move_cash(
        start,
        debit=jnp.asarray([0, 2]),
        credit=jnp.asarray([1, 1]),
        amount=jnp.asarray([[7, 8, 9], [1, 2, 3]]),
        row_of_world=_WORLD,
    )

    assert moved.sum(axis=0).tolist() == start.sum(axis=0).tolist()


def test_a_scalar_counterparty_accumulates_every_flow_facing_it() -> None:
    """One counterparty facing many flows — which is what `rest_of_world` is for a month of
    sales. The scalar side takes the SUM, not the last write, so N sales crediting N accounts
    debit the world once for the whole N."""

    moved = _move_cash(
        _empty(),
        debit=_WORLD,
        credit=jnp.asarray([0, 1, 2]),
        amount=jnp.asarray([[5, 0, 0], [6, 0, 0], [7, 0, 0]]),
        row_of_world=_WORLD,
    )

    assert moved[_WORLD].tolist() == [-18, 0, 0]
    assert moved[:, 0].tolist() == [5, 6, 7, -18]


def test_an_unresolved_counterparty_settles_against_the_rest_of_the_world() -> None:
    """A `-1` row means "outside the model", and outside the model is a real row.

    This is the discriminating case: `_scatter_rows`, which every phase used before, redirects a
    `-1` into a padding row it then slices off — so the money leaves one side and arrives
    nowhere. Both are asserted below, because the point is not that `_move_cash` handles `-1`
    but that it handles it DIFFERENTLY.
    """

    amount = jnp.asarray([[11, 0, 0]])
    rows = jnp.asarray([-1])

    moved = _move_cash(_empty(), debit=0, credit=rows, amount=amount, row_of_world=_WORLD)
    dropped = _scatter_rows(_empty(), rows, amount)

    assert moved[_WORLD].tolist() == [11, 0, 0]
    assert moved.sum().item() == 0
    assert np.all(np.asarray(dropped) == 0)


def test_an_unresolved_row_on_both_sides_is_a_no_op_rather_than_a_leak() -> None:
    """Padding rows in a `(month, slot)` table carry `-1` on BOTH legs with a zero amount. They
    must stay harmless once both legs resolve to the same real row."""

    moved = _move_cash(
        _empty(),
        debit=jnp.asarray([-1]),
        credit=jnp.asarray([-1]),
        amount=jnp.asarray([[0, 0, 0]]),
        row_of_world=_WORLD,
    )

    assert moved.tolist() == _empty().tolist()


def test_move_cash_is_traceable() -> None:
    """It runs inside the `lax.scan`, so a numpy op creeping in would be a defect the eager cases
    above cannot see. `row_of_world` is static, which is what lets a caller pass a plain int."""

    jitted = jax.jit(lambda cash, amount: _move_cash(cash, debit=0, credit=1, amount=amount, row_of_world=_WORLD))

    assert jitted(_empty(), jnp.asarray([4, 5, 6]))[1].tolist() == [4, 5, 6]


if __name__ == "__main__":
    pytest_bazel.main()
