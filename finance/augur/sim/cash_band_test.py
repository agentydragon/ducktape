"""Invariants of the (s,S) cash band."""

from __future__ import annotations

import numpy as np
import pytest
import pytest_bazel

from finance.augur.sim.cash_band import cash_order


def _order(cash: list[int], outflow: list[int], floor: int, ceiling: int) -> tuple[list[int], list[int]]:
    result = cash_order(
        cash_cents=np.asarray(cash, dtype=np.int64),
        scheduled_outflow_cents=np.asarray(outflow, dtype=np.int64),
        floor_cents=np.full(len(cash), floor, dtype=np.int64),
        ceiling_cents=np.full(len(cash), ceiling, dtype=np.int64),
    )
    return [int(x) for x in result.raise_cents], [int(x) for x in result.invest_cents]


def test_inside_the_band_nothing_happens() -> None:
    """The band is a no-trade region. Cash drifting within it must not provoke a sale, or
    the agent trades every month and pays tax for the privilege."""

    assert _order([500], [0], 100, 1_000) == ([0], [0])


def test_crossing_the_floor_refills_to_the_ceiling_not_the_floor() -> None:
    """The far edge is the whole point. Projected cash of 50 against a [100, 1000] band
    raises 950, not 50 — refilling only to the floor would put the agent back at the
    trigger next month and make it a forced seller into every dip."""

    assert _order([50], [0], 100, 1_000) == ([950], [0])


def test_crossing_the_ceiling_invests_down_to_the_floor() -> None:
    """The mirror image: 5,000 against a [100, 1000] band invests 4,900, leaving the floor
    intact rather than draining to zero or stopping at the ceiling."""

    assert _order([5_000], [0], 100, 1_000) == ([0], [4_900])


def test_landing_exactly_on_a_bound_is_inside_the_band() -> None:
    """Bounds are inclusive, so sitting exactly on one is not a crossing. Otherwise a
    portfolio parked on the floor trades every single month forever."""

    assert _order([100, 1_000], [0, 0], 100, 1_000) == ([0, 0], [0, 0])


def test_the_decision_is_made_against_the_projected_end_of_month_balance() -> None:
    """Cash of 900 looks comfortable inside a [100, 1000] band — until you count the 850 of
    obligations already scheduled for the month. Deciding on the balance BEFORE the bills
    would leave the agent short and force a second trade, or a false ruin."""

    assert _order([900], [850], 100, 1_000) == ([950], [0])


def test_obligations_can_turn_an_over_ceiling_balance_into_a_raise() -> None:
    """The projection cuts both ways: 5,000 of cash with 4,950 of bills is not a surplus to
    invest, it is a shortfall to fund. A band read off raw cash would buy here and then
    default."""

    assert _order([5_000], [4_950], 100, 1_000) == ([950], [0])


def test_a_raise_covers_the_obligations_as_well_as_the_refill() -> None:
    """What is raised has to clear the month's bills AND restore the buffer, since the
    obligations settle out of the same account after the sale lands."""

    raised, _ = _order([0], [10_000], 100, 1_000)

    assert raised == [11_000]
    # Post-obligation balance is exactly the ceiling: 0 + 11_000 - 10_000.
    assert raised[0] - 10_000 == 1_000


def test_a_negative_projection_is_funded_back_to_the_ceiling() -> None:
    """Cash can project negative when bills exceed the balance. The raise must cover the
    hole and the refill, not just the distance to the floor."""

    assert _order([0], [3_000], 100, 1_000) == ([4_000], [0])


def test_the_two_sides_are_mutually_exclusive() -> None:
    """A band with floor <= ceiling cannot be crossed both ways at once. If both sides ever
    fired, the agent would sell and buy in the same month and pay tax to stand still."""

    rng = np.random.default_rng(20260805)
    cash = rng.integers(-50_000, 500_000, size=256, dtype=np.int64)
    outflow = rng.integers(0, 200_000, size=256, dtype=np.int64)

    order = cash_order(
        cash_cents=cash,
        scheduled_outflow_cents=outflow,
        floor_cents=np.full(256, 25_000, dtype=np.int64),
        ceiling_cents=np.full(256, 90_000, dtype=np.int64),
    )

    assert np.all((order.raise_cents == 0) | (order.invest_cents == 0))
    assert np.all(order.raise_cents >= 0)
    assert np.all(order.invest_cents >= 0)


def test_acting_on_the_order_lands_inside_the_band() -> None:
    """The property that makes it a band at all: whatever the starting balance, executing
    the order leaves projected cash within [floor, ceiling]."""

    rng = np.random.default_rng(1)
    cash = rng.integers(-50_000, 500_000, size=256, dtype=np.int64)
    outflow = rng.integers(0, 200_000, size=256, dtype=np.int64)
    floor = np.full(256, 25_000, dtype=np.int64)
    ceiling = np.full(256, 90_000, dtype=np.int64)

    order = cash_order(cash_cents=cash, scheduled_outflow_cents=outflow, floor_cents=floor, ceiling_cents=ceiling)
    settled = cash - outflow + order.raise_cents - order.invest_cents

    assert np.all(settled >= floor)
    assert np.all(settled <= ceiling)


def test_per_rollout_bands_are_independent() -> None:
    """Bounds are per-rollout because they can be CPI-indexed, and inflation differs by
    path. A band broadcast from one rollout to all would silently apply one path's
    purchasing power to every other."""

    order = cash_order(
        cash_cents=np.asarray([500, 500], dtype=np.int64),
        scheduled_outflow_cents=np.asarray([0, 0], dtype=np.int64),
        floor_cents=np.asarray([100, 600], dtype=np.int64),
        ceiling_cents=np.asarray([1_000, 2_000], dtype=np.int64),
    )

    assert [int(x) for x in order.raise_cents] == [0, 1_500]


def test_an_inverted_band_is_rejected() -> None:
    """A ceiling below its floor has no interior, so every balance crosses both bounds and
    the policy would sell and buy forever. Better to reject the config than to run it."""

    with pytest.raises(ValueError, match="floor must not exceed"):
        cash_order(
            cash_cents=np.asarray([0], dtype=np.int64),
            scheduled_outflow_cents=np.asarray([0], dtype=np.int64),
            floor_cents=np.asarray([1_000], dtype=np.int64),
            ceiling_cents=np.asarray([100], dtype=np.int64),
        )


if __name__ == "__main__":
    pytest_bazel.main()
