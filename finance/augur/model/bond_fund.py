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
from pydantic import Field, NonNegativeFloat, PositiveFloat

from finance.augur.model.schemas import FrozenModel
from finance.augur.model.series import SecuritySymbol

MONTHS_PER_YEAR = 12

MINIMUM_ANNUAL_YIELD = 0.0001
"""Existing one-basis-point floor applied by yield-construction helpers.

This is a modeling approximation, not a requirement of the valuation formula,
which supports zero yields and negative yields greater than -1.
"""


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


class BondFundSpec(FrozenModel):
    """A constant-maturity fund and its chosen reference-yield construction.

    Both historical and generated yields can price this same fund. Maturity is an
    input; duration follows from the bond math. Zero maturity represents cash.
    Static spread/ratio adjustments are modeling assumptions, not fitted dynamics
    or tax rules; distribution tax character belongs to the simulation scenario.
    """

    symbol: SecuritySymbol
    maturity_years: NonNegativeFloat
    initial_price_usd: PositiveFloat = 100.0
    yield_curve: YieldCurve = YieldCurve.GOVERNMENT
    spread: float = Field(default=0.0, description="Additive annualized-decimal spread after the curve ratio.")
    curve_ratio: PositiveFloat = Field(
        default=1.0, description="Scale the reference yield before adding the spread, e.g. a static muni/taxable ratio."
    )


def government_curve_yield(short_rate: np.ndarray, term_spread: np.ndarray, *, maturity_years: float) -> np.ndarray:
    """Linear short-to-10y interpolation, flat beyond 10y; not a full term-structure model."""

    return short_rate + min(maturity_years / 10.0, 1.0) * term_spread


def fund_yield(spec: BondFundSpec, reference_yield: np.ndarray) -> np.ndarray:
    """Apply the fund's ratio, then additive spread, then the positive-yield guard."""

    return np.maximum(spec.curve_ratio * reference_yield + spec.spread, MINIMUM_ANNUAL_YIELD)


def par_bond_price(coupon_rate: np.ndarray, market_yield: np.ndarray, *, maturity_years: float) -> np.ndarray:
    """Price, per 1.0 of face, of a bond paying `coupon_rate` and discounted at `market_yield`.

    The textbook annuity-plus-redemption present value. Exactly 1.0 when the two rates agree,
    which is what makes "issued at par" a definition here rather than an approximation — and
    what a duration step `exp(-D·Δy)` only gets right to first order.

    Finite yields must exceed -1 under this annual-compounding convention.
    Zero yield values the coupons without discounting; it is not an error or a
    reason to invent a positive yield. Fractional maturity retains the proxy's
    annuity interpolation, not a dated stub-coupon schedule.
    """

    coupon = np.asarray(coupon_rate, dtype=np.float64)
    yields = np.asarray(market_yield, dtype=np.float64)
    if not np.isfinite(maturity_years) or maturity_years < 0:
        raise ValueError("maturity_years must be finite and nonnegative")
    if not np.all(np.isfinite(coupon)):
        raise ValueError("coupon rates must be finite")
    if not np.all(np.isfinite(yields)) or np.any(yields <= -1):
        raise ValueError("market yields must be finite and greater than -1")

    log_discount = -maturity_years * np.log1p(yields)
    # expm1 avoids cancellation near zero; the annuity's exact limit there is T.
    annuity = np.divide(-np.expm1(log_discount), yields, out=np.full_like(yields, maturity_years), where=yields != 0)
    return np.asarray(coupon * annuity + np.exp(log_discount))


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

    market_yield = np.asarray(market_yield, dtype=np.float64)
    price = np.empty_like(market_yield)
    distribution = np.empty_like(market_yield)
    price[:, 0] = initial_price_usd
    # This proxy's opening-month convention pays at the initial yield on the initial mark.
    # Experiments opening before the first accrual can explicitly set this payout to zero.
    distribution[:, 0] = initial_price_usd * market_yield[:, 0] / MONTHS_PER_YEAR

    for month in range(1, market_yield.shape[1]):
        coupon_rate = market_yield[:, month - 1]
        repriced = par_bond_price(coupon_rate, market_yield[:, month], maturity_years=maturity_years)
        price[:, month] = price[:, month - 1] * repriced
        distribution[:, month] = price[:, month - 1] * coupon_rate / MONTHS_PER_YEAR

    return price, distribution
