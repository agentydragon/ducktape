"""Cash-budget proposals preserve exact counts and leave execution to the policy."""

from typing import Any

import pytest
import pytest_bazel

from finance.augur.policy.cash_band import Hold, Invest, Raise, cash_band


@pytest.mark.parametrize(
    ("projected", "expected"),
    [
        (-40, Raise(amount=240)),
        (99, Raise(amount=101)),
        (100, Hold()),
        (150, Hold()),
        (200, Hold()),
        (201, Invest(amount=101)),
    ],
)
def test_far_edge_proposals_and_inclusive_bounds(projected: int, expected: Raise | Invest | Hold) -> None:
    assert cash_band(projected_cash=projected, floor=100, ceiling=200) == expected


@pytest.mark.parametrize(("projected", "expected"), [(-1, Raise(amount=1)), (0, Hold()), (1, Invest(amount=1))])
def test_zero_band(projected: int, expected: Raise | Invest | Hold) -> None:
    assert cash_band(projected_cash=projected, floor=0, ceiling=0) == expected


def test_integer_precision_and_largest_representable_proposals() -> None:
    assert cash_band(projected_cash=2**53 + 1, floor=2**53, ceiling=2**53) == Invest(amount=1)
    assert cash_band(projected_cash=-(2**63) + 1, floor=0, ceiling=0) == Raise(amount=2**63 - 1)
    assert cash_band(projected_cash=2**63 - 1, floor=0, ceiling=0) == Invest(amount=2**63 - 1)
    assert cash_band(projected_cash=2**63 - 1, floor=2**63 - 1, ceiling=2**63 - 1) == Hold()


@pytest.mark.parametrize(("floor", "ceiling"), [(-1, 0), (2, 1)])
def test_invalid_band(floor: int, ceiling: int) -> None:
    with pytest.raises(ValueError, match="cash band floor"):
        cash_band(projected_cash=0, floor=floor, ceiling=ceiling)


@pytest.mark.parametrize("value", [1.0, True, "1"])
@pytest.mark.parametrize("field", ["projected_cash", "floor", "ceiling"])
def test_noninteger_counts_are_rejected(field: str, value: Any) -> None:
    values = {"projected_cash": 0, "floor": 0, "ceiling": 0}
    values[field] = value
    with pytest.raises(TypeError, match="integer currency-quanta"):
        cash_band(**values)


@pytest.mark.parametrize(
    ("projected", "floor", "ceiling"),
    [(2**63, 0, 0), (-(2**63) - 1, 0, 0), (0, 2**63, 2**63), (0, 0, 2**63), (-(2**63), 0, 0), (-1, 0, 2**63 - 1)],
)
def test_unrepresentable_inputs_and_proposals(projected: int, floor: int, ceiling: int) -> None:
    with pytest.raises(OverflowError, match="64-bit money"):
        cash_band(projected_cash=projected, floor=floor, ceiling=ceiling)


if __name__ == "__main__":
    pytest_bazel.main()
