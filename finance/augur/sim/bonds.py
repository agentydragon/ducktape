"""Contractual coupon dates and exact fixed nominal payments for par-held bonds.

Compilation determines a nominal coupon once from its annual PPB rate. Indexed
payments instead depend on current principal and remain runtime calculations.
These functions do not price tradable bonds or apply cash/tax effects.
"""

from __future__ import annotations

from finance.augur.sim.fixed_point import MONEY_FACTOR_SCALE

MONTHS_PER_YEAR = 12


def coupon_months(*, purchase_month_index: int, maturity_month_index: int, coupon_period_months: int) -> list[int]:
    """Months on which this bond pays, maturity included.

    Counted forward from purchase, which is the same schedule as counting back from
    maturity because the config requires the term to be a whole number of periods.
    """

    return list(range(purchase_month_index + coupon_period_months, maturity_month_index + 1, coupon_period_months))


def coupon_amount_quanta(*, face_quanta: int, annual_coupon_rate_ppb: int, coupon_period_months: int) -> int:
    """Fixed nominal coupon: round the full rational once, half up, to money quanta.

    The annual rate is already quantized to the simulator's PPB grid. Do not round
    a periodic rate first; nondivisible periods can change the resulting payment.
    """

    values = (face_quanta, annual_coupon_rate_ppb, coupon_period_months)
    if any(not isinstance(value, int) or isinstance(value, bool) for value in values):
        raise TypeError("coupon terms require integer counts")
    if face_quanta < 0 or annual_coupon_rate_ppb < 0 or coupon_period_months <= 0:
        raise ValueError("face/rate must be nonnegative and coupon period positive")
    if any(value >= 1 << 63 for value in values):
        raise OverflowError("coupon terms do not fit signed 64-bit counts")
    numerator = face_quanta * annual_coupon_rate_ppb * coupon_period_months
    denominator = MONEY_FACTOR_SCALE * MONTHS_PER_YEAR
    coupon = (2 * numerator + denominator) // (2 * denominator)
    if coupon >= 1 << 63:
        raise OverflowError("coupon does not fit signed 64-bit money")
    return coupon


def is_on_books(*, month_index: int, purchase_month_index: int, maturity_month_index: int) -> bool:
    """Whether the bond is still an asset at the END of `month_index`.

    Maturity is EXCLUSIVE. The face is redeemed into cash during the maturity month, so by
    the time that month's balance sheet is struck the position is cash, not a bond. Counting
    the maturity month as held would double-count the face — once as the bond and once as
    the cash it just became.
    """

    return purchase_month_index <= month_index < maturity_month_index
