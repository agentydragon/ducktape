"""The policy as a box: observations in, actions out.

Numbers are chosen so each split is exact and hand-checkable — the sleeve arithmetic is
tested on its own in `allocation_test.py`, so what matters here is the COMPOSITION: that
the band's sizing and the allocation's placement are joined the right way round, and that
the properties which only exist once they are joined actually hold.
"""

from __future__ import annotations

import jax
import jax.numpy as jnp
import numpy as np
import pytest
import pytest_bazel

from finance.augur.sim.actor_policy import ActorActions, SleeveUniverse, decide
from finance.augur.sim.actor_view import ActorSlots, build_actor_view

_SCALE = np.asarray([100, 100], dtype=np.int64)
_PURCHASE_MONTH = np.asarray([0, 0], dtype=np.int64)
# Sleeve values land at 900 and 100 cents: quantity * price // scale.
_QUANTITY = [[900], [100]]
_PRICE = [[100], [100]]

_UNIVERSE = SleeveUniverse(weights=np.asarray([1, 1], dtype=np.int64), lot_rows=((0,), (1,)), funding_cash_row=0)


def _view(*, funding_cash: int, other_cash: int = 50_000, outflow: int = 0):
    return build_actor_view(
        month=jnp.asarray(3, dtype=jnp.int64),
        slots=ActorSlots(cash_slots=(0, 1), lot_slots=(0, 1), external_cash_slot=2, cash_count=2, lot_count=2),
        cash_cents=jnp.asarray([[funding_cash], [other_cash]], dtype=jnp.int64),
        lot_quantity=jnp.asarray(_QUANTITY, dtype=jnp.int64),
        lot_cost_basis_per_unit_cents=jnp.asarray([[50], [50]], dtype=jnp.int64),
        lot_unit_price_cents=jnp.asarray(_PRICE, dtype=jnp.int64),
        lot_quantity_scale=_SCALE,
        lot_purchase_month=_PURCHASE_MONTH,
        scheduled_outflow_cents=jnp.asarray([outflow], dtype=jnp.int64),
    )


def _act(*, funding_cash: int, other_cash: int = 50_000, outflow: int = 0, floor: int = 100, ceiling: int = 1_000):
    return decide(
        view=_view(funding_cash=funding_cash, other_cash=other_cash, outflow=outflow),
        universe=_UNIVERSE,
        floor_cents=jnp.asarray([floor], dtype=jnp.int64),
        ceiling_cents=jnp.asarray([ceiling], dtype=jnp.int64),
    )


def _flat(actions: ActorActions) -> tuple[list[int], list[int], list[int]]:
    return (
        [int(x) for x in actions.sell_cents[:, 0]],
        [int(x) for x in actions.buy_cents[:, 0]],
        [int(x) for x in actions.spend_cents],
    )


def test_a_quiet_month_inside_the_band_emits_nothing() -> None:
    """The composed no-trade guarantee, and the one most easily lost. Drift is corrected
    only through cashflow that was going to happen anyway; a policy that trades on drift
    alone would pay tax every month to chase a ratio nobody asked it to hit exactly."""

    assert _flat(_act(funding_cash=500)) == ([0, 0], [0, 0], [0])


def test_crossing_the_floor_sells_from_the_overweight_sleeve() -> None:
    """Sizing and placement joined: projected cash of 50 against [100, 1000] raises 950,
    and water-filling 950 out of sleeves worth 900 and 100 leaves both at 25 — so the
    overweight sleeve gives up 875 and the other 75."""

    sell, buy, _ = _flat(_act(funding_cash=50))

    assert sell == [875, 75]
    assert sum(sell) == 950
    assert buy == [0, 0]


def test_crossing_the_ceiling_buys_into_the_underweight_sleeve() -> None:
    """The mirror: 5,000 against [100, 1000] invests 4,900 down to the floor, and filling
    the laggard first leaves both sleeves at 2,950."""

    sell, buy, _ = _flat(_act(funding_cash=5_000))

    assert buy == [2_050, 2_850]
    assert sum(buy) == 4_900
    assert sell == [0, 0]


def test_the_raise_covers_the_months_obligations_too() -> None:
    """Cash of 900 looks fine inside [100, 1000] until the 850 of bills already scheduled
    for the month are counted. Sizing on the raw balance would fund nothing and then
    default; sizing on the projected balance raises 950."""

    sell, _, _ = _flat(_act(funding_cash=900, outflow=850))

    assert sum(sell) == 950


def test_selling_and_buying_cannot_both_happen_in_one_month() -> None:
    """A property of the COMPOSITION, not of either half: the band has an interior, so its
    two sides are mutually exclusive, so the policy can never pay tax to stand still.
    Fuzzed, because this is the invariant most likely to break if the wiring is rearranged.
    """

    rng = np.random.default_rng(20260805)
    for cash in rng.integers(-5_000, 50_000, size=64, dtype=np.int64):
        for outflow in (0, 400, 5_000):
            sell, buy, _ = _flat(_act(funding_cash=int(cash), outflow=outflow))
            assert sum(sell) == 0 or sum(buy) == 0


def test_the_band_reads_the_funding_account_not_the_agents_total_cash() -> None:
    """Obligations settle from one account, so the band has to watch that account. Treating
    an agent's balances as fungible would report 50,050 of cash here, see no crossing, and
    quietly let the funding account run dry."""

    sell, _, _ = _flat(_act(funding_cash=50, other_cash=50_000))

    assert sum(sell) == 950


def test_a_sell_target_is_capped_by_what_the_sleeves_hold() -> None:
    """Sleeves worth 1,000 in total cannot fund a 9,900 raise. The policy asks for
    everything and no more, leaving the caller to fail the month — inventing the difference
    here would hide a ruin behind a sale that never happened."""

    sell, _, _ = _flat(_act(funding_cash=-9_000))

    assert sell == [900, 100]


def test_this_policy_never_spends() -> None:
    """`spend_cents` exists so the action language is complete, but a funding policy has no
    view on lifestyle. A tier-aware policy fills it; this one emitting anything non-zero
    would mean spending had leaked into the wrong box."""

    for cash in (50, 500, 5_000):
        assert _flat(_act(funding_cash=cash))[2] == [0]


def test_rollouts_are_decided_independently() -> None:
    """One call, one answer per rollout. A rollout below its floor and one above its
    ceiling must get opposite actions from the same invocation."""

    actions = decide(
        view=build_actor_view(
            month=jnp.asarray(3, dtype=jnp.int64),
            slots=ActorSlots(cash_slots=(0,), lot_slots=(0, 1), external_cash_slot=1, cash_count=1, lot_count=2),
            cash_cents=jnp.asarray([[50, 5_000]], dtype=jnp.int64),
            lot_quantity=jnp.asarray([[900, 900], [100, 100]], dtype=jnp.int64),
            lot_cost_basis_per_unit_cents=jnp.asarray([[50, 50], [50, 50]], dtype=jnp.int64),
            lot_unit_price_cents=jnp.asarray([[100, 100], [100, 100]], dtype=jnp.int64),
            lot_quantity_scale=_SCALE,
            lot_purchase_month=_PURCHASE_MONTH,
            scheduled_outflow_cents=jnp.asarray([0, 0], dtype=jnp.int64),
        ),
        universe=_UNIVERSE,
        floor_cents=jnp.asarray([100, 100], dtype=jnp.int64),
        ceiling_cents=jnp.asarray([1_000, 1_000], dtype=jnp.int64),
    )

    assert [int(x) for x in actions.sell_cents.sum(axis=0)] == [950, 0]
    assert [int(x) for x in actions.buy_cents.sum(axis=0)] == [0, 4_900]


def test_the_policy_traces_under_jit() -> None:
    """It runs inside the scan, so the whole composition has to trace — not just the pieces
    separately. `universe` is closed over, exactly as the engine will hold it."""

    decided = jax.jit(
        lambda view, floor, ceiling: decide(view=view, universe=_UNIVERSE, floor_cents=floor, ceiling_cents=ceiling)
    )(_view(funding_cash=50), jnp.asarray([100], dtype=jnp.int64), jnp.asarray([1_000], dtype=jnp.int64))

    assert [int(x) for x in decided.sell_cents[:, 0]] == [875, 75]


def test_a_lot_cannot_belong_to_two_sleeves() -> None:
    """A double-counted lot inflates the portfolio total and skews every target — silently,
    since the arithmetic stays internally consistent."""

    with pytest.raises(ValueError, match="more than one sleeve"):
        SleeveUniverse(weights=np.asarray([1, 1], dtype=np.int64), lot_rows=((0, 1), (1,)), funding_cash_row=0)


def test_weights_and_sleeves_must_agree() -> None:
    with pytest.raises(ValueError, match="3 weights but 2 lot groups"):
        SleeveUniverse(weights=np.asarray([1, 1, 1], dtype=np.int64), lot_rows=((0,), (1,)), funding_cash_row=0)


if __name__ == "__main__":
    pytest_bazel.main()
