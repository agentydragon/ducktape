"""What an actor may and may not see.

Most of these are about the visibility rule rather than arithmetic: an observation that
quietly includes another agent's cash, the contra row, or next month's price would make
every policy built on it look better than it is, and would do so silently.
"""

from __future__ import annotations

import jax.numpy as jnp
import numpy as np
import pytest
import pytest_bazel

from finance.augur.sim.actor_view import ActorSlots, build_actor_view

# `(lot, R)`: the purchase month is per-rollout carried state, because a slot a policy chose
# to fill is bought in a different month in each rollout. Rollout 1 bought lot 0 three months
# later than rollout 0, so the holding period differs across the row.
_PURCHASE_MONTH = np.asarray([[0, 3], [3, 3], [3, 3]], dtype=np.int64)


def _view(*, month: int = 6, cash_slots: tuple[int, ...] = (0, 1), lot_slots: tuple[int, ...] = (0, 1)):
    return build_actor_view(
        month=jnp.asarray(month, dtype=jnp.int64),
        slots=ActorSlots(cash_slots=cash_slots, lot_slots=lot_slots, external_cash_slot=3, cash_count=4, lot_count=3),
        # Row 2 is another agent; row 3 is `rest_of_world`.
        cash_cents=jnp.asarray([[100, 200], [10, 20], [7_000, 8_000], [-9_999, -9_999]], dtype=jnp.int64),
        lot_quantity=jnp.asarray([[500, 1_000], [200, 400], [9_999, 9_999]], dtype=jnp.int64),
        lot_cost_basis_per_unit_cents=jnp.asarray([[50, 60], [70, 80], [1, 1]], dtype=jnp.int64),
        # Marked value, computed by the caller (see `build_actor_view`). Every row is
        # distinct so a slice that grabbed the wrong lot is visible; row 2 is another agent's.
        lot_value_cents=jnp.asarray([[500, 2_000], [600, 1_600], [777, 888]], dtype=jnp.int64),
        lot_purchase_month=_PURCHASE_MONTH,
        scheduled_outflow_cents=jnp.asarray([11, 22], dtype=jnp.int64),
        # Two tradable instruments, priced whether or not they are held.
        instrument_price_cents=jnp.asarray([[100, 110], [20, 22]], dtype=jnp.int64),
        instrument_quantity_scale=jnp.asarray([1, 100], dtype=jnp.int64),
    )


def test_every_field_carries_the_rollout_axis() -> None:
    """The contract is a struct of arrays, not an array of structs: one call decides for
    every rollout. If a field ever lost its rollout axis, the policy would be silently
    broadcasting one path's answer across all of them."""

    view = _view()

    assert view.cash_cents.shape == (2, 2)
    assert view.lot_quantity.shape == view.lot_value_cents.shape == (2, 2)
    assert view.lot_cost_basis_per_unit_cents.shape == view.lot_holding_months.shape == (2, 2)
    assert view.scheduled_outflow_cents.shape == (2,)


def test_only_the_agents_own_rows_are_visible() -> None:
    """The other agent's 7,000/8,000 and the contra row's -9,999 must not appear, and
    neither must the third lot."""

    view = _view()

    assert [[int(x) for x in row] for row in view.cash_cents] == [[100, 200], [10, 20]]
    assert [int(x) for x in view.total_cash_cents] == [110, 220]
    assert [[int(x) for x in row] for row in view.lot_quantity] == [[500, 1_000], [200, 400]]


def test_value_and_quantity_are_sliced_by_the_same_rows() -> None:
    """Value is marked by the caller, so what the view owes is ALIGNMENT: lot i's value must
    be lot i's, not some other lot's.

    Every input row here is distinct, so a slice off by one would pair lot 0's 500 quanta
    with lot 1's 600 cents and every allocation computed from it would be wrong while every
    shape stayed right. Row 2 belongs to another agent and its 777/888 must not appear at all.
    """

    view = _view(lot_slots=(1, 0))

    assert [[int(x) for x in row] for row in view.lot_quantity] == [[200, 400], [500, 1_000]]
    assert [[int(x) for x in row] for row in view.lot_value_cents] == [[600, 1_600], [500, 2_000]]


def test_holding_period_is_months_since_acquisition() -> None:
    """Exposed so a policy can weigh the long/short boundary itself rather than reaching
    into the engine's gain classification."""

    view = _view(month=6)

    assert [[int(x) for x in row] for row in view.lot_holding_months] == [[6, 3], [3, 3]]


def test_the_month_reaches_only_the_holding_period() -> None:
    """No clairvoyance, stated as narrowly as the builder now permits.

    Marks arrive already resolved for `month`, so the builder holds no price cube it could
    index past the current month — that guarantee moved to the caller with the valuation.
    What remains here is that `month` reaches exactly ONE output: advancing it with identical
    state must change the holding period and nothing else.
    """

    early, late = _view(month=6), _view(month=7)

    assert np.array_equal(np.asarray(early.lot_value_cents), np.asarray(late.lot_value_cents))
    assert np.array_equal(np.asarray(early.cash_cents), np.asarray(late.cash_cents))
    assert not np.array_equal(np.asarray(early.lot_holding_months), np.asarray(late.lot_holding_months))


def test_sleeves_aggregate_over_view_rows() -> None:
    """Sleeve grouping indexes the VIEW's lot axis, which has already narrowed to this
    agent. Grouping by plan indices would read another agent's lots."""

    sleeves = _view().sleeve_value_cents(((0,), (1,)))

    assert [[int(x) for x in row] for row in sleeves] == [[500, 2_000], [600, 1_600]]
    assert [int(x) for x in sleeves.sum(axis=0)] == [1_100, 3_600]


def test_the_external_contra_row_cannot_be_granted() -> None:
    """The load-bearing check, and it fires at CONSTRUCTION — there is no separate validate
    call to forget. `rest_of_world` holds money that LEFT the modeled world, so an actor able
    to see it would read its own past spending as an asset."""

    with pytest.raises(ValueError, match="external contra row"):
        ActorSlots(cash_slots=(0, 3), lot_slots=(0,), external_cash_slot=3, cash_count=4, lot_count=3)


def test_out_of_range_slots_are_rejected() -> None:
    with pytest.raises(ValueError, match="cash slot out of range"):
        ActorSlots(cash_slots=(0, 9), lot_slots=(0,), external_cash_slot=3, cash_count=4, lot_count=3)
    with pytest.raises(ValueError, match="lot slot out of range"):
        ActorSlots(cash_slots=(0,), lot_slots=(9,), external_cash_slot=3, cash_count=4, lot_count=3)


def test_duplicate_slots_are_rejected() -> None:
    """A repeated row would double-count that account or lot in every total the policy
    computes — an easy mistake to make when building slot lists from a config, and a
    silent one afterwards."""

    with pytest.raises(ValueError, match="duplicate cash slot"):
        ActorSlots(cash_slots=(0, 0), lot_slots=(0,), external_cash_slot=3, cash_count=4, lot_count=3)
    with pytest.raises(ValueError, match="duplicate lot slot"):
        ActorSlots(cash_slots=(0,), lot_slots=(1, 1), external_cash_slot=3, cash_count=4, lot_count=3)


def test_an_agent_with_no_lots_still_produces_a_well_shaped_view() -> None:
    """A pure-cash actor is a real configuration, and it must not need a special case
    downstream — the lot axis is simply empty."""

    view = _view(lot_slots=())

    assert view.lot_quantity.shape == (0, 2)
    assert [int(x) for x in view.total_cash_cents] == [110, 220]


if __name__ == "__main__":
    pytest_bazel.main()
