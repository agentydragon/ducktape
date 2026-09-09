"""Dated cashflow valuation, independent of a forecast or holding policy."""

import jax
import jax.numpy as jnp
import numpy as np
import pytest
import pytest_bazel

from finance.augur.model.nominal_bond import par_coupon_rate, price_per_face


def test_discounted_cashflows_use_each_payment_date_and_include_principal() -> None:
    # A deliberately non-flat curve: $2 at 6m, $2 at 12m, and $102 at 18m.
    discounts = jnp.array([[0.99, 0.96, 0.91], [1.01, 1.02, 1.03]])
    prices = jax.jit(price_per_face, static_argnames="coupon_period_months")(
        jnp.array([0.04, 0.04]), discounts, coupon_period_months=6
    )
    np.testing.assert_allclose(prices, [0.02 * (0.99 + 0.96) + 1.02 * 0.91, 0.02 * (1.01 + 1.02) + 1.02 * 1.03])


def test_zero_rates_have_no_singularity_or_positive_yield_floor() -> None:
    value = price_per_face(jnp.array([0.04]), jnp.ones((1, 3)), coupon_period_months=12)
    assert float(value[0]) == pytest.approx(1.12)


def test_redemption_removes_the_asset_even_with_a_nonzero_coupon() -> None:
    value = jax.jit(price_per_face, static_argnames="coupon_period_months")(
        jnp.array([0.04, 0.06]), jnp.empty((2, 0)), coupon_period_months=12
    )
    np.testing.assert_array_equal(value, [0, 0])


def test_par_coupon_reprices_to_one_on_each_curve_in_a_batch() -> None:
    discounts = jnp.array([[0.99, 0.96, 0.91], [1.01, 1.02, 1.03], [1.0, 1.0, 1.0]])
    coupons = jax.jit(par_coupon_rate, static_argnames="coupon_period_months")(discounts, coupon_period_months=6)
    np.testing.assert_allclose(price_per_face(coupons, discounts, coupon_period_months=6), 1, atol=1e-14)
    assert float(coupons[1]) < 0


if __name__ == "__main__":
    pytest_bazel.main()
