"""Bond mechanics. Pure functions over a bond's terms; no engine state, no randomness.

A bond is modelled as its **cashflow schedule** — a coupon every `coupon_period_months`,
and the face returned at maturity. Nothing here computes a price, and that is deliberate:
a price is the sum of those cashflows times discount factors, and phase 1 has no discount
curve.

Phase 1 buys **at par and holds to maturity**, which is what makes the missing curve
sound rather than a gap:

- At par there is no discount or premium, so there is nothing to amortize and book value
  is the face for the bond's whole life. (Effective-interest amortization would need the
  purchase yield — itself a discount factor — so "amortized cost" is not a way to avoid
  the curve, it is the curve wearing a different name.)
- Held to maturity, the bond is never marked, so no intermediate price is ever needed.
- Redemption at par against a par basis is a zero-gain event, so no capital-gain path is
  involved either.

The coupon is the only income the instrument produces, and it is `InterestIncome` tagged
with the issuer's jurisdiction — which is what lets a Treasury be state-exempt and a
municipal bond federally exempt without the instrument knowing who holds it.

Yields never appear in the simulator at all. `HistoricalSeries` enforces strictly-positive
levels, and yields go negative, so a yield cannot be a level series — the type system
rejects it. When phase 2 adds marking, it adds *discount factors*, which are positive.
"""

from __future__ import annotations

from fractions import Fraction

MONTHS_PER_YEAR = 12


def coupon_months(*, purchase_month_index: int, maturity_month_index: int, coupon_period_months: int) -> list[int]:
    """Months on which this bond pays, maturity included.

    Counted forward from purchase, which is the same schedule as counting back from
    maturity because the config requires the term to be a whole number of periods.
    """

    return list(range(purchase_month_index + coupon_period_months, maturity_month_index + 1, coupon_period_months))


def coupon_amount_cents(*, face_cents: int, annual_coupon_rate: float, coupon_period_months: int) -> int:
    """One period's coupon, in cents, from a face ALREADY in cents.

    Takes cents rather than dollars so the dollars→cents conversion happens once, at the
    compile boundary, and the same integer face drives both the coupon and the redemption.
    Taking dollars here meant converting the same face twice by two different routes, which
    is exactly the kind of duplication that agrees until one side changes.

    The arithmetic is exact: `Fraction` is a pair of integers, and a rate written as a
    decimal literal converts to one without going through binary floating point. So the
    only rounding is the single deliberate one at the end.

    Rounded once, here, rather than accrued and rounded at each payment: every coupon on a
    given bond is the identical integer, so a 30-year ladder cannot drift.
    """

    exact = Fraction(face_cents) * Fraction(str(annual_coupon_rate)) * Fraction(coupon_period_months, MONTHS_PER_YEAR)
    # Half-up on a non-negative rational, in integers: coupon rates and face are both
    # non-negative, so there is no half-away-from-zero case to worry about.
    return (2 * exact.numerator + exact.denominator) // (2 * exact.denominator)


def is_on_books(*, month_index: int, purchase_month_index: int, maturity_month_index: int) -> bool:
    """Whether the bond is still an asset at the END of `month_index`.

    Maturity is EXCLUSIVE. The face is redeemed into cash during the maturity month, so by
    the time that month's balance sheet is struck the position is cash, not a bond. Counting
    the maturity month as held would double-count the face — once as the bond and once as
    the cash it just became.
    """

    return purchase_month_index <= month_index < maturity_month_index
