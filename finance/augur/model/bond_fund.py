"""Price a constant-maturity bond fund from its own yield, the way the standard sources do.

A fund of this shape holds a par bond of fixed maturity, collects its coupon, and each period
sells it and buys a fresh one at the prevailing yield. That makes it the one fixed-income
object a portfolio policy can treat like a stock: perpetual, fungible, divisible, marked every
month, and "sell $X of it" is an order somebody can actually place. An individual bond is none
of those — it matures, on a date, with a fixed cashflow schedule — which is why `BondHolding`
models that separately and marks it illiquid.

**Only the fund's own yield is needed**, not a curve: a constant-maturity fund lives at one
point on it. Pricing a bond of arbitrary maturity, or rolling a ladder, does need the curve —
see `model/SPEC.md` gaps 8 and 10 — and neither is this.

**The convention is the reference implementation's**, deliberately. Aswath Damodaran's
historical returns dataset — the free series most published work uses where Ibbotson's SBBI is
paywalled — computes exactly this: "the promised coupon at the start of the year and the price
change due to interest rate changes", repricing at the SAME maturity each period. Repricing at
a maturity one period shorter would add a roll-down return, which is real; it is left out here
so this reproduces the reference bit for bit (`bond_fund_test.py`) rather than being a third,
unvalidatable variant. Annual-pay coupons, for the same reason.
"""

from __future__ import annotations

from enum import StrEnum

import numpy as np

MONTHS_PER_YEAR = 12


class YieldCurve(StrEnum):
    """Which observed yield an instrument is priced off.

    A fund's own yield is the only input its price needs, so this names it rather than deriving
    it from a government curve plus a guessed credit spread. `CORPORATE_AAA` is what a
    high-grade corporate sleeve actually earned, month by month back to 1919, and the gap to
    `CORPORATE_BAA` is the credit spread as an observation instead of a constant.
    """

    GOVERNMENT = "government"
    CORPORATE_AAA = "corporate_aaa"
    CORPORATE_BAA = "corporate_baa"


def par_bond_price(coupon_rate: np.ndarray, market_yield: np.ndarray, *, maturity_years: float) -> np.ndarray:
    """Price, per 1.0 of face, of a bond paying `coupon_rate` and discounted at `market_yield`.

    The textbook annuity-plus-redemption present value. Exactly 1.0 when the two rates agree,
    which is what makes "issued at par" a definition here rather than an approximation — and
    what a duration step `exp(-D·Δy)` only gets right to first order.
    """

    discount = (1.0 + market_yield) ** -maturity_years
    return np.asarray(coupon_rate * (1.0 - discount) / market_yield + discount)


def constant_maturity_fund_paths(
    market_yield: np.ndarray, *, maturity_years: float, initial_price_usd: float
) -> tuple[np.ndarray, np.ndarray]:
    """`(price, distribution_per_unit)` in dollars per unit, from an annualized-decimal yield.

    `market_yield` is `(rollout, month)`. Month `t`'s bond was bought at par at month `t-1`, so
    it carries `market_yield[t-1]` as its coupon and is marked at `market_yield[t]`; the fund
    then rolls into a fresh par bond, buying the face its net asset value affords.

    That last clause is the whole of what the payout rides on. Face per unit is NOT constant —
    it tracks the mark, because a fund whose mark has fallen can only buy the face it can pay
    for. A model that pins face while the mark moves has the fund distributing a yield on its
    own net assets that drifts away from the yield of the bonds it holds, without limit and in
    the direction that flatters it. See <../debug/bond_sleeve_overdistribution.md>.
    """

    price = np.empty_like(market_yield)
    distribution = np.empty_like(market_yield)
    price[:, 0] = initial_price_usd
    # Month 0 has no prior month to have bought at, so the position opens at the month's own
    # yield on the initial mark. It therefore pays the same as month 1, whose mark has not
    # moved yet. A zero would read more naturally as "nothing accrued", but the engine rejects
    # a non-positive value in a `security_distribution:` series outright — a restriction a
    # payout does not obviously need, unlike the price series it is grouped with (#5832).
    distribution[:, 0] = initial_price_usd * market_yield[:, 0] / MONTHS_PER_YEAR

    for month in range(1, market_yield.shape[1]):
        coupon_rate = market_yield[:, month - 1]
        repriced = par_bond_price(coupon_rate, market_yield[:, month], maturity_years=maturity_years)
        price[:, month] = price[:, month - 1] * repriced
        distribution[:, month] = price[:, month - 1] * coupon_rate / MONTHS_PER_YEAR

    return price, distribution
