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

from finance.augur.sim.actor_view import ActorSlots, build_actor_view
from finance.augur.sim.target_allocation import SleeveOrders, SleeveUniverse, _quanta_for_cents, decide

_PURCHASE_MONTH = np.asarray([0, 0], dtype=np.int64)
# Sleeve 0 is worth 900 cents and sleeve 1 is worth 100, against equal weights — so sleeve 0
# is the overweight one and every sale should come out of it first.
_QUANTITY = [[900], [100]]
_VALUE = [[900], [100]]

_UNIVERSE = SleeveUniverse(weights=np.asarray([1, 1], dtype=np.int64), lot_rows=((0,), (1,)), funding_cash_row=0)


def _view(*, funding_cash: int, other_cash: int = 50_000, outflow: int = 0):
    return build_actor_view(
        month=jnp.asarray(3, dtype=jnp.int64),
        slots=ActorSlots(cash_slots=(0, 1), lot_slots=(0, 1), external_cash_slot=2, cash_count=2, lot_count=2),
        cash_cents=jnp.asarray([[funding_cash], [other_cash]], dtype=jnp.int64),
        lot_quantity=jnp.asarray(_QUANTITY, dtype=jnp.int64),
        lot_cost_basis_per_unit_cents=jnp.asarray([[50], [50]], dtype=jnp.int64),
        lot_value_cents=jnp.asarray(_VALUE, dtype=jnp.int64),
        lot_purchase_month=_PURCHASE_MONTH,
        scheduled_outflow_cents=jnp.asarray([outflow], dtype=jnp.int64),
        # A cent a quantum, so an order's quanta and the cents it raises read the same and the
        # composition stays hand-checkable. `_quanta_for_cents` is exercised on its own below.
        instrument_price_cents=jnp.asarray([[1], [1]], dtype=jnp.int64),
        instrument_quantity_scale=jnp.asarray([1, 1], dtype=jnp.int64),
    )


def _act(
    *,
    funding_cash: int,
    other_cash: int = 50_000,
    outflow: int = 0,
    floor: int = 100,
    ceiling: int = 1_000,
    rebalance_tolerance: float | None = None,
):
    return decide(
        view=_view(funding_cash=funding_cash, other_cash=other_cash, outflow=outflow),
        universe=_UNIVERSE,
        floor_cents=jnp.asarray([floor], dtype=jnp.int64),
        ceiling_cents=jnp.asarray([ceiling], dtype=jnp.int64),
        rebalance_tolerance=rebalance_tolerance,
    )


def _flat(orders: SleeveOrders) -> tuple[list[int], list[int]]:
    return ([int(x) for x in orders.sell_quanta[:, 0]], [int(x) for x in orders.buy_quanta[:, 0]])


def test_a_quiet_month_inside_the_band_emits_nothing() -> None:
    """The composed no-trade guarantee, and the one most easily lost. By default drift is
    corrected only through cashflow that was going to happen anyway; a policy that traded on
    drift alone would pay tax every month to chase a ratio nobody asked it to hit exactly.

    The portfolio here is 900/100 against equal weights — as far off target as this fixture
    gets — so it is exactly the state a drift rebalance WOULD trade in. That is the point:
    without a configured tolerance it emits nothing at all.
    """

    assert _flat(_act(funding_cash=500)) == ([0, 0], [0, 0])


def test_crossing_the_floor_sells_from_the_overweight_sleeve() -> None:
    """Sizing and placement joined: projected cash of 50 against [100, 1000] raises 950,
    and water-filling 950 out of sleeves worth 900 and 100 leaves both at 25 — so the
    overweight sleeve gives up 875 and the other 75."""

    sell, buy = _flat(_act(funding_cash=50))

    assert sell == [875, 75]
    assert sum(sell) == 950
    assert buy == [0, 0]


def test_crossing_the_ceiling_buys_into_the_underweight_sleeve() -> None:
    """The mirror: 5,000 against [100, 1000] invests 4,900 down to the floor, and filling
    the laggard first leaves both sleeves at 2,950."""

    sell, buy = _flat(_act(funding_cash=5_000))

    assert buy == [2_050, 2_850]
    assert sum(buy) == 4_900
    assert sell == [0, 0]


def test_the_raise_covers_the_months_obligations_too() -> None:
    """Cash of 900 looks fine inside [100, 1000] until the 850 of bills already scheduled
    for the month are counted. Sizing on the raw balance would fund nothing and then
    default; sizing on the projected balance raises 950."""

    sell, _ = _flat(_act(funding_cash=900, outflow=850))

    assert sum(sell) == 950


def test_selling_and_buying_cannot_both_happen_in_one_month() -> None:
    """A property of the COMPOSITION, not of either half: the band has an interior, so its
    two sides are mutually exclusive, so the policy can never pay tax to stand still.
    Fuzzed, because this is the invariant most likely to break if the wiring is rearranged.
    """

    rng = np.random.default_rng(20260805)
    for cash in rng.integers(-5_000, 50_000, size=64, dtype=np.int64):
        for outflow in (0, 400, 5_000):
            sell, buy = _flat(_act(funding_cash=int(cash), outflow=outflow))
            assert sum(sell) == 0 or sum(buy) == 0


def test_the_band_reads_the_funding_account_not_the_agents_total_cash() -> None:
    """Obligations settle from one account, so the band has to watch that account. Treating
    an agent's balances as fungible would report 50,050 of cash here, see no crossing, and
    quietly let the funding account run dry."""

    sell, _ = _flat(_act(funding_cash=50, other_cash=50_000))

    assert sum(sell) == 950


def test_a_sell_target_is_capped_by_what_the_sleeves_hold() -> None:
    """Sleeves worth 1,000 in total cannot fund a 9,900 raise. The policy asks for
    everything and no more, leaving the caller to fail the month — inventing the difference
    here would hide a ruin behind a sale that never happened."""

    sell, _ = _flat(_act(funding_cash=-9_000))

    assert sell == [900, 100]


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
            lot_value_cents=jnp.asarray([[900, 900], [100, 100]], dtype=jnp.int64),
            lot_purchase_month=_PURCHASE_MONTH,
            scheduled_outflow_cents=jnp.asarray([0, 0], dtype=jnp.int64),
            instrument_price_cents=jnp.asarray([[1, 1], [1, 1]], dtype=jnp.int64),
            instrument_quantity_scale=jnp.asarray([1, 1], dtype=jnp.int64),
        ),
        universe=_UNIVERSE,
        floor_cents=jnp.asarray([100, 100], dtype=jnp.int64),
        ceiling_cents=jnp.asarray([1_000, 1_000], dtype=jnp.int64),
    )

    assert [int(x) for x in actions.sell_quanta.sum(axis=0)] == [950, 0]
    assert [int(x) for x in actions.buy_quanta.sum(axis=0)] == [0, 4_900]


def test_the_policy_traces_under_jit() -> None:
    """It runs inside the scan, so the whole composition has to trace — not just the pieces
    separately. `universe` is closed over, exactly as the engine will hold it."""

    decided = jax.jit(
        lambda view, floor, ceiling: decide(view=view, universe=_UNIVERSE, floor_cents=floor, ceiling_cents=ceiling)
    )(_view(funding_cash=50), jnp.asarray([100], dtype=jnp.int64), jnp.asarray([1_000], dtype=jnp.int64))

    assert [int(x) for x in decided.sell_quanta[:, 0]] == [875, 75]


def test_a_lot_cannot_belong_to_two_sleeves() -> None:
    """A double-counted lot inflates the portfolio total and skews every target — silently,
    since the arithmetic stays internally consistent."""

    with pytest.raises(ValueError, match="more than one sleeve"):
        SleeveUniverse(weights=np.asarray([1, 1], dtype=np.int64), lot_rows=((0, 1), (1,)), funding_cash_row=0)


def test_weights_and_sleeves_must_agree() -> None:
    with pytest.raises(ValueError, match="3 weights but 2 lot groups"):
        SleeveUniverse(weights=np.asarray([1, 1, 1], dtype=np.int64), lot_rows=((0,), (1,)), funding_cash_row=0)


# -- Pricing an order ----------------------------------------------------------------------


def _quanta(cents: int, price_cents: int, scale: int, round_up: bool = True) -> int:
    return int(
        _quanta_for_cents(
            cents=jnp.asarray([[cents]], dtype=jnp.int64),
            unit_price_cents=jnp.asarray([[price_cents]], dtype=jnp.int64),
            quantity_scale=jnp.asarray([[scale]], dtype=jnp.int64),
            round_up=round_up,
        )[0, 0]
    )


def test_an_order_is_never_a_quantum_short_of_the_ask() -> None:
    """The reason the conversion ceils. At 3 cents a unit a 10-cent ask needs 4 units, not the
    3 that flooring would give — and 3 units raise 9 cents, leaving the month a cent short.

    Under a zero-width band that cent is not cosmetic: the raise IS the shortfall, so an
    obligation goes unpaid and the rollout fails for an arithmetic artifact."""

    assert _quanta(cents=10, price_cents=3, scale=1) == 4
    assert _quanta(cents=9, price_cents=3, scale=1) == 3


@pytest.mark.parametrize("scale", [1, 100, 100_000_000])
@pytest.mark.parametrize("price_cents", [1, 7, 333, 5_000_000])
def test_an_order_covers_its_ask_across_scales_and_prices(scale: int, price_cents: int) -> None:
    """Swept rather than spot-checked. Whether a division lands exactly depends on the
    (price, scale) pair, so a single example proves nothing about the rest — which is how an
    earlier version of this arithmetic passed inspection while undershooting."""

    for cents in (1, 999, 1_000_000, 123_456_789):
        quanta = _quanta(cents=cents, price_cents=price_cents, scale=scale)
        # What the engine will pay for those quanta, by its own valuation.
        assert quanta * price_cents / scale >= cents, f"{cents=} {price_cents=} {scale=}"


def test_an_unpriceable_sleeve_orders_nothing() -> None:
    """A sleeve with no modeled price series reads price 0. Unpriceable is not free: dividing
    by it would either explode or hand over an unbounded quantity for nothing."""

    assert _quanta(cents=1_000, price_cents=0, scale=1) == 0


def test_a_zero_ask_orders_nothing_however_it_is_priced() -> None:
    """Ceiling division turns any positive numerator into at least one quantum, so a zero ask
    has to be special-cased or every quiet month would trade a single share."""

    assert _quanta(cents=0, price_cents=12_345, scale=1) == 0


def test_a_purchase_never_spends_more_than_it_was_given() -> None:
    """The reason the buy side floors where the sell side ceils. The cents a buy is sized against
    are the cents ABOVE the floor the policy is keeping; buying a 4th unit at 3 cents to place a
    10-cent deposit spends 12, and the 2 cents come out of the floor the band just promised."""

    assert _quanta(cents=10, price_cents=3, scale=1, round_up=False) == 3
    assert _quanta(cents=9, price_cents=3, scale=1, round_up=False) == 3


@pytest.mark.parametrize("scale", [1, 100, 100_000_000])
@pytest.mark.parametrize("price_cents", [1, 7, 333, 5_000_000])
def test_a_purchase_stays_within_budget_across_scales_and_prices(scale: int, price_cents: int) -> None:
    """Swept for the same reason the sell side is: whether the division lands exactly depends on
    the (price, scale) pair, and overspending by a quantum at a five-figure unit price is a
    four-figure overdraft."""

    for cents in (1, 999, 1_000_000, 123_456_789):
        quanta = _quanta(cents=cents, price_cents=price_cents, scale=scale, round_up=False)
        assert quanta * price_cents / scale <= cents, f"{cents=} {price_cents=} {scale=}"


def test_an_unpriceable_sleeve_buys_nothing() -> None:
    assert _quanta(cents=1_000, price_cents=0, scale=1, round_up=False) == 0


# -- Rebalancing on drift alone ------------------------------------------------------------


def test_a_configured_tolerance_trades_in_a_quiet_month() -> None:
    """The same quiet month as above, with a tolerance configured. 900/100 against equal
    weights is 500/500 on target, so 400 crosses — and this is the first time the policy emits
    a sell and a buy in the same month, which the band alone could never do."""

    assert _flat(_act(funding_cash=500, rebalance_tolerance=0.25)) == ([400, 0], [0, 400])


def test_a_rebalance_is_suppressed_while_the_band_is_raising() -> None:
    """Not merely additive. The raise already water-fills out of the overweight sleeve, which
    is the best rebalance that cashflow can buy; adding a drift trade on top would sell sleeve
    0 to raise cash and buy sleeve 1 with cash the month needs to SPEND.

    The assertion is the unchanged raise, not "no buy": a rebalance that fired here would also
    change the sell side, so asserting only the buy would miss half of it.
    """

    assert _flat(_act(funding_cash=50, rebalance_tolerance=0.25)) == _flat(_act(funding_cash=50))


def test_a_rebalance_is_suppressed_while_the_band_is_investing() -> None:
    """The mirror. The deposit already fills the underweight sleeve first."""

    assert _flat(_act(funding_cash=5_000, rebalance_tolerance=0.25)) == _flat(_act(funding_cash=5_000))


def test_a_tolerance_wide_enough_to_cover_the_drift_still_trades_nothing() -> None:
    """Configuring a rebalance is not the same as asking for one every month. The fixture is
    80% off target, so a tolerance above that leaves it alone."""

    assert _flat(_act(funding_cash=500, rebalance_tolerance=1.0)) == ([0, 0], [0, 0])


def test_a_rebalance_sell_is_still_capped_by_the_holding() -> None:
    """The cap belongs to the sell side as a whole, not to the raise path that first needed it.
    Sleeve 0 is worth 900 cents at a cent a quantum, so a trim can ask for at most 900 quanta —
    and this asserts the cap is applied AFTER the two sell sources are summed, which is the
    only place it can be applied once."""

    orders = _act(funding_cash=500, rebalance_tolerance=0.25)

    assert int(orders.sell_quanta[0, 0]) <= 900


if __name__ == "__main__":
    pytest_bazel.main()
