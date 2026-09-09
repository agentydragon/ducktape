"""Value regular nominal bond cashflows against supplied discount factors.

The caller selects the remaining coupon dates and supplies one discount factor per
date, with principal due on the last date. Dates on or before valuation are excluded:
the result is a dirty price AFTER that month's payments, not a clean quote. Curve
generation, trading, tax basis, defaults and settlement calendars are outside this module.
"""

import jax
import jax.numpy as jnp

from finance.augur.model.float64 import LEVEL_DTYPE


def price_per_face(
    annual_coupon_rate: jax.Array, discount_factors: jax.Array, *, coupon_period_months: int
) -> jax.Array:
    """Batch over leading axes; the last discount-factor axis contains future coupons.

    An empty payment axis means the bond has redeemed and is worth zero. Coupons
    use equal month-based accrual periods; no day-count or ex-coupon calendar is implied.
    """
    if coupon_period_months <= 0:
        raise ValueError("coupon_period_months must be positive")
    discounts = jnp.asarray(discount_factors, dtype=LEVEL_DTYPE)
    if discounts.shape[-1] == 0:
        return jnp.zeros(discounts.shape[:-1], dtype=LEVEL_DTYPE)
    coupon = jnp.asarray(annual_coupon_rate, dtype=LEVEL_DTYPE) * coupon_period_months / 12
    return coupon * jnp.sum(discounts, axis=-1) + discounts[..., -1]


def par_coupon_rate(discount_factors: jax.Array, *, coupon_period_months: int) -> jax.Array:
    """Coupon making a newly issued regular bond worth one unit of face.

    Requires positive discount factors on a nonempty, regular coupon schedule.
    A negative result is possible; whether such issuance is allowed is a product choice.
    """
    if coupon_period_months <= 0 or discount_factors.shape[-1] == 0:
        raise ValueError("par issuance needs a positive coupon period and at least one payment")
    discounts = jnp.asarray(discount_factors, dtype=LEVEL_DTYPE)
    return (1 - discounts[..., -1]) / jnp.sum(discounts, axis=-1) * 12 / coupon_period_months
