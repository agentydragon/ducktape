"""Trace the financial differences between holding, selling, and replacing dated bonds."""

from collections.abc import Callable

import numpy as np
import pytest
import pytest_bazel

from finance.augur.x.bond_policies.construction import (
    Action,
    compare_constructions,
    construct,
    hold,
    roll_annually,
    sell_at_18,
    stipulated_curves,
)


@pytest.fixture
def curves() -> np.ndarray:
    return np.stack(list(stipulated_curves().values()))


def test_rate_shocks_change_marks_not_contractual_coupons_or_redemption(curves: np.ndarray) -> None:
    result = construct(curves, hold)
    np.testing.assert_allclose(result.price[:, 0], 100)
    assert result.price[1, 6] < 100 < result.price[2, 6]
    # The rate rise changes the discounting, not the remaining promised $4/$104.
    assert result.price[1, 12] == pytest.approx(4 / 1.08 + 104 / 1.08**2)
    np.testing.assert_allclose(result.coupon[:, [12, 24, 36]], 4)
    np.testing.assert_allclose(result.coupon.sum(axis=1), 12)
    np.testing.assert_allclose(result.redemption[:, 36], 100)
    np.testing.assert_allclose(result.redemption.sum(axis=1), 100)
    np.testing.assert_array_equal(result.bond_value[:, 36:], 0)
    np.testing.assert_allclose(result.cash[:, 36:], 100)
    np.testing.assert_array_equal(result.sale_proceeds, 0)


def test_early_sale_uses_dirty_value_and_ends_coupon_and_principal_claims(curves: np.ndarray) -> None:
    result = construct(curves, sell_at_18)
    # Half a year remains until the next coupon; accrued interest is included in PV.
    proceeds = 4 / 1.08**0.5 + 104 / 1.08**1.5
    assert result.sale_proceeds[1, 18] == pytest.approx(proceeds)
    np.testing.assert_allclose(result.cash[1, 18:], proceeds)
    np.testing.assert_array_equal(result.bond_value[:, 18:], 0)
    np.testing.assert_array_equal(result.coupon[:, 18:], 0)
    np.testing.assert_array_equal(result.redemption, 0)
    np.testing.assert_allclose(result.coupon.sum(axis=1), 4)


def test_roll_is_self_financing_and_new_face_determines_subsequent_coupons(curves: np.ndarray) -> None:
    result = construct(curves, roll_annually)
    replacement_dates = np.arange(12, 73, 12)
    np.testing.assert_allclose(result.purchases[:, replacement_dates], result.sale_proceeds[:, replacement_dates])
    np.testing.assert_allclose(result.price[:, replacement_dates], result.purchases[:, replacement_dates])
    np.testing.assert_array_equal(result.cash, 0)
    np.testing.assert_array_equal(result.redemption, 0)
    # Year 1 sells the aged two-year bond and buys a new three-year bond, both at 4% coupon.
    sale = 4 / 1.08 + 104 / 1.08**2
    new_price_per_face = 0.04 / 1.08 + 0.04 / 1.08**2 + 1.04 / 1.08**3
    assert result.coupon[1, 24] == pytest.approx(sale / new_price_per_face * 0.04)
    assert result.coupon[1, 24] != pytest.approx(result.coupon[1, 12])


def test_same_flat_curve_does_not_make_the_constructions_equivalent(curves: np.ndarray) -> None:
    arms = compare_constructions(curves)
    # The fund continues paying after the original dated bond has redeemed to idle cash.
    assert arms["hold"].coupon[0, 48] == 0
    assert arms["roll_annually"].coupon[0, 48] == pytest.approx(4)
    assert arms["constant_maturity_proxy"].coupon[0, 48] == pytest.approx(4 / 12)
    # All start BEFORE the first earned coupon, including the explicitly adapted proxy.
    for arm in arms.values():
        np.testing.assert_array_equal(arm.coupon[:, 0], 0)


@pytest.mark.parametrize("policy", [hold, sell_at_18, roll_annually])
def test_batch_composition_and_future_curves_do_not_change_a_paths_past(
    curves: np.ndarray, policy: Callable[[int], Action]
) -> None:
    original = construct(curves, policy)
    alone = construct(curves[1:2], policy)
    np.testing.assert_array_equal(original.price[1:2], alone.price)
    np.testing.assert_array_equal(original.coupon[1:2], alone.coupon)
    changed = curves.copy()
    changed[:, 25:, 1:] *= 0.8
    perturbed = construct(changed, policy)
    np.testing.assert_array_equal(original.price[:, :25], perturbed.price[:, :25])
    np.testing.assert_array_equal(original.coupon[:, :25], perturbed.coupon[:, :25])


def test_existing_bond_prices_through_zero_and_negative_rates_without_a_floor(curves: np.ndarray) -> None:
    curves[:, 6:] = 1
    zero = construct(curves, hold)
    assert zero.price[0, 6] == pytest.approx(112)
    curves[:, 6:, 1:] = 1.01
    negative = construct(curves, hold)
    assert negative.price[0, 6] == pytest.approx(113.12)
    with pytest.raises(ValueError, match="proxy needs positive yields"):
        compare_constructions(curves)


@pytest.mark.parametrize("invalid", [0, -1, float("nan"), float("inf")])
def test_invalid_discount_factors_are_rejected_before_construction(curves: np.ndarray, invalid: float) -> None:
    curves[1, 6, 12] = invalid
    with pytest.raises(ValueError, match="finite and positive"):
        construct(curves, hold)


if __name__ == "__main__":
    pytest_bazel.main()
