"""Compose dated-bond investment strategies on a shared batch of discount curves.

This is an instrument-model experiment: one strategy unit owns dated bonds and idle
cash, distributing coupons. Trading functions live here; household funding remains
in the Rust engine. Redemptions and sale proceeds stay in unit NAV, never masquerade
as interest distributions. Month zero opens the investment without paying a coupon.
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto

import jax.numpy as jnp
import numpy as np

from finance.augur.model.bond_fund import constant_maturity_fund_paths
from finance.augur.model.nominal_bond import par_coupon_rate, price_per_face
from finance.augur.sim.bonds import coupon_months

MATURITY_MONTHS = 36
COUPON_PERIOD_MONTHS = 12
INITIAL_PRICE = 100.0
ANNUAL_COUPON_RATE = 0.04


@dataclass(frozen=True)
class DatedConstruction:
    """Per original strategy unit, after each month's cashflows and trading.

    Coupon is paid out; principal and trading cash stay inside the strategy. Flows
    are positive amounts received/spent, while bond_value and cash are closing stocks.
    """

    bond_value: np.ndarray
    cash: np.ndarray
    coupon: np.ndarray
    sale_proceeds: np.ndarray
    purchases: np.ndarray
    redemption: np.ndarray

    @property
    def price(self) -> np.ndarray:
        return np.asarray(self.bond_value + self.cash)


@dataclass(frozen=True)
class ProxyConstruction:
    """Reference proxy's observable NAV/payout only; no invented underlying trade log."""

    price: np.ndarray
    coupon: np.ndarray


class Action(Enum):
    HOLD = auto()
    SELL = auto()
    ROLL = auto()


def hold(month: int) -> Action:
    return Action.HOLD


def sell_at_18(month: int) -> Action:
    return Action.SELL if month == 18 else Action.HOLD


def roll_annually(month: int) -> Action:
    return Action.ROLL if month > 0 and month % 12 == 0 else Action.HOLD


def stipulated_curves() -> dict[str, np.ndarray]:
    """Illustrative stresses, not forecasts or draws from an empirical distribution.

    Annual-effective zero rates produce discount factors on integer monthly tenors.
    Every path opens flat at 4%; the shock occurs at month 6. The final snapshot is
    valuation-only when passed to the household engine.
    """
    tenors = np.arange(MATURITY_MONTHS + 1)
    flat = np.full((74, MATURITY_MONTHS + 1), 0.04)
    up = flat.copy()
    up[6:] = 0.08
    down = flat.copy()
    down[6:] = 0.01
    zero = flat.copy()
    zero[6:] = 0.0
    twist = flat.copy()
    twist[6:] = 0.02 + 0.06 * tenors / MATURITY_MONTHS
    return {
        name: (1 + rates) ** (-tenors / 12)
        for name, rates in (("flat", flat), ("rise", up), ("fall", down), ("steepen", twist), ("zero", zero))
    }


def _validate_curves(discount_factors: np.ndarray) -> None:
    if (
        discount_factors.ndim != 3
        or discount_factors.shape[0] == 0
        or discount_factors.shape[1] < 2
        or discount_factors.shape[2] <= MATURITY_MONTHS
    ):
        raise ValueError("curves need (rollout, snapshot, tenor), including tenors 0 through 36 months")
    if not np.all(np.isfinite(discount_factors)) or np.any(discount_factors <= 0):
        raise ValueError("discount factors must be finite and positive")
    if not np.all(discount_factors[:, :, 0] == 1):
        raise ValueError("zero-tenor discount factors must equal one")


def construct(discount_factors: np.ndarray, policy: Callable[[int], Action]) -> DatedConstruction:
    """Batch a scheduled trading function over already materialized curves.

    Each purchase is a fresh 3-year, annual-pay 4% coupon bond, possibly off par.
    Face adjusts to the cash available. Existing coupons never reset when rates move.
    Trading follows payments and uses dirty value of remaining dated cashflows.
    Policies see only the current month; this example does not promise feedback policies.
    """
    _validate_curves(discount_factors)
    shape = discount_factors.shape[:2]
    result = DatedConstruction(
        bond_value=np.zeros(shape),
        cash=np.zeros(shape),
        coupon=np.zeros(shape),
        sale_proceeds=np.zeros(shape),
        purchases=np.zeros(shape),
        redemption=np.zeros(shape),
    )
    face = np.zeros(shape[0])
    cash = np.full(shape[0], INITIAL_PRICE)
    issued = 0

    def value_per_face(month: int, issue: int) -> np.ndarray:
        remaining = [
            due - month
            for due in coupon_months(
                purchase_month_index=issue,
                maturity_month_index=issue + MATURITY_MONTHS,
                coupon_period_months=COUPON_PERIOD_MONTHS,
            )
            if due > month
        ]
        return np.asarray(
            price_per_face(
                jnp.asarray(ANNUAL_COUPON_RATE),
                jnp.asarray(discount_factors[:, month, remaining]),
                coupon_period_months=COUPON_PERIOD_MONTHS,
            )
        )

    for month in range(shape[1]):
        age = month - issued
        if 0 < age <= MATURITY_MONTHS and age % COUPON_PERIOD_MONTHS == 0:
            result.coupon[:, month] = face * ANNUAL_COUPON_RATE * COUPON_PERIOD_MONTHS / 12
        if age == MATURITY_MONTHS:
            result.redemption[:, month] = face
            cash = cash + face
            face = np.zeros_like(face)

        mark = face * value_per_face(month, issued)
        action = Action.ROLL if month == 0 else policy(month)
        if action in (Action.SELL, Action.ROLL):
            result.sale_proceeds[:, month] = mark
            cash = cash + mark
            face = np.zeros_like(face)
            mark = np.zeros_like(mark)
        if action is Action.ROLL:
            issued = month
            face = cash / value_per_face(month, issued)
            result.purchases[:, month] = cash
            mark = cash
            cash = np.zeros_like(cash)
        result.bond_value[:, month] = mark
        result.cash[:, month] = cash
    return result


def compare_constructions(discount_factors: np.ndarray) -> dict[str, DatedConstruction | ProxyConstruction]:
    """Reuse every supplied curve across policy arms and the existing reference proxy.

    The proxy consumes 3-year annual par yields derived from these curves; it retains
    its own same-maturity repricing and monthly payout conventions. Its zero-month
    payout is suppressed because this experiment opens the investment at month zero.
    """
    _validate_curves(discount_factors)
    yields = np.asarray(
        par_coupon_rate(
            jnp.asarray(discount_factors[:, :, COUPON_PERIOD_MONTHS : MATURITY_MONTHS + 1 : COUPON_PERIOD_MONTHS]),
            coupon_period_months=COUPON_PERIOD_MONTHS,
        )
    )
    if np.any(yields < 0):
        raise ValueError(
            "the constant-maturity proxy does not support negative-coupon issuance; no floor is applied here"
        )
    price, coupon = constant_maturity_fund_paths(
        yields, maturity_years=MATURITY_MONTHS / 12, initial_price_usd=INITIAL_PRICE
    )
    coupon[:, 0] = 0
    return {
        "hold": construct(discount_factors, hold),
        "sell_at_18": construct(discount_factors, sell_at_18),
        "roll_annually": construct(discount_factors, roll_annually),
        "constant_maturity_proxy": ProxyConstruction(price=price, coupon=coupon),
    }
