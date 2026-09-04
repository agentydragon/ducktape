"""Reduced-form tax-loss-harvesting (TLH) yield model — Piece 2 core.

This is the calibratable math behind a direct-indexing harvest process: given the
period's index return and a position's embedded-gain fraction, it produces the
*gross realized loss* harvested this period as a fraction of market value. It does
**not** touch the sim engine — `augur/sim/engine` wires it in (see the
`_apply_tlh_harvest` phase), reading MV/basis and the index path per rollout,
clamping the output to the loss actually available below basis, and feeding it
into the Piece-1 capital-loss netting.

Why reduced-form (not constituent simulation): a single S&P 500 series has no
cross-sectional dispersion, so harvestable losses must be modeled as a calibrated
function of the index path Augur already samples rather than emerging from
hundreds of simulated names. `HarvestPolicy` documents the boundary and
upgrade path.

Shape and calibration anchor: harvesting is **front-loaded**. A cash-funded
account starts with cost basis = market value (embedded-gain fraction `e ≈ 0`) and
harvests near its peak; as winners appreciate and harvested losers are reset away,
the position becomes dominated by low-basis winners (`e → 1`) and harvestable
losses dry up ("ossification"). Yield therefore decays from a peak toward a floor
as `(1 - e) ** maturity_decay_exponent`, and is amplified in drawdowns. This shape
is taken from Vanguard's "Tax-loss harvesting: Why a personalized approach is
important" (July 2024); the magnitude of TLH alpha and the wash-sale haircut are
bounded by Chaudhuri, Burnham & Lo, "An Empirical Evaluation of Tax-Loss-Harvesting
Alpha," Financial Analysts Journal 76(3) 2020. Remaining follow-ups live in
`finance/augur/sim/TODO.md`.

All parameters are `[HEURISTIC]`: with only the account's first-year (TY2025)
1099-B there is no in-account history to fit the decay rate, so the curve's shape
comes from the external research above and the level is anchored to the first-year
1099-B (~5%/yr gross, essentially all short-term). Re-fit as future years' forms
arrive.
"""

from __future__ import annotations

# ruff: noqa: F722 -- jaxtyping shape strings are not Python forward-reference expressions.
import numpy as np
from jaxtyping import Array, Bool, Float64, Int64
from pydantic import BaseModel, ConfigDict, Field, model_validator

MONTHS_PER_YEAR = 12

# Parts per billion: the fixed-point scale every dimensionless rate in the simulator shares
# (`fixed_point.MONEY_FACTOR_SCALE`, and `RATE_SCALE_PPB` on the Rust side).
PPB = 1_000_000_000


def to_ppb(value: float, *, field: str) -> int:
    """Quantize one configured rate to PPB, refusing a value that would not survive it."""

    scaled = round(value * PPB)
    if abs(scaled / PPB - value) > 1e-15:
        raise ValueError(f"{field} ({value}) is not representable in parts per billion")
    return scaled


def _select(
    condition: Bool[np.ndarray, " rollout"] | Bool[Array, " rollout"],
    when_true: Int64[np.ndarray, " rollout"] | Int64[Array, " rollout"],
    when_false: Int64[np.ndarray, " rollout"] | Int64[Array, " rollout"],
) -> Int64[np.ndarray, " rollout"] | Int64[Array, " rollout"]:
    """`where` written in operators, so one formula runs eager and traced alike."""

    return when_false + (when_true - when_false) * condition.astype(np.int64)


def _mul_ppb(
    left: Int64[np.ndarray, " rollout"] | Int64[Array, " rollout"] | int,
    right: Int64[np.ndarray, " rollout"] | Int64[Array, " rollout"] | int,
) -> Int64[np.ndarray, " rollout"] | Int64[Array, " rollout"]:
    """Multiply two non-negative PPB factors, rounding the result half up.

    Splitting `left` at the scale keeps the intermediate product under `PPB**2` rather
    than forming `left * right` directly, which a large sensitivity would overflow.
    Algebraically identical to rounding `left * right / PPB` in one wider step, which is
    how the Rust engine spells it (`mul_div_round_half_up` over `i128`).
    """

    return (left // PPB) * right + ((left % PPB) * right + PPB // 2) // PPB


def _isqrt(
    value: Int64[np.ndarray, " rollout"] | Int64[Array, " rollout"],
) -> Int64[np.ndarray, " rollout"] | Int64[Array, " rollout"]:
    """Exact `floor(sqrt(value))` for `0 <= value <= PPB**2`, in int64.

    Being exact is the whole point: the Rust engine calls `i64::isqrt`, and two exact
    implementations of a floor agree without either mirroring the other's algorithm.
    float64 cannot represent every int64 below `PPB**2`, so the square-root seed can land
    a little off; the Newton steps and the bounded corrections after them close that gap.
    `test_isqrt_matches_math_isqrt` is what actually holds this to `math.isqrt`.
    """

    zero = value * 0
    one = zero + 1
    guess = _select(value > 0, (value.astype(np.float64) ** 0.5).astype(np.int64), one)
    for _ in range(2):
        guess = _select(guess > 0, guess, one)
        guess = (guess + value // _select(guess > 0, guess, one)) // 2
    guess = _select(guess > 0, guess, zero)
    for _ in range(2):
        guess = _select(guess * guess > value, guess - 1, guess)
    for _ in range(2):
        guess = _select((guess + 1) * (guess + 1) <= value, guess + 1, guess)
    return _select(value > 0, guess, zero)


def _pow_half_ppb(
    base: Int64[np.ndarray, " rollout"] | Int64[Array, " rollout"], half_exponent: int
) -> Int64[np.ndarray, " rollout"] | Int64[Array, " rollout"]:
    """`base ** (half_exponent / 2)` in PPB, exactly.

    A half-integer exponent is a whole number of multiplications and at most one square
    root, both exact in integers — which is why `HarvestYieldParams` admits only those.
    `half_exponent` is a policy constant, so the loop unrolls at trace time.
    """

    power = base * 0 + PPB
    for _ in range(half_exponent // 2):
        power = _mul_ppb(power, base)
    if half_exponent % 2 == 0:
        return power
    return _mul_ppb(power, _isqrt(base * PPB))


def harvest_fraction_curve_ppb(
    embedded_gain_ppb: Int64[np.ndarray, " rollout"] | Int64[Array, " rollout"],
    drawdown_ppb: Int64[np.ndarray, " rollout"] | Int64[Array, " rollout"],
    *,
    peak_annual_yield_ppb: int,
    floor_annual_yield_ppb: int,
    maturity_decay_half_exponent: int,
    drawdown_sensitivity_ppb: int,
) -> Int64[np.ndarray, " rollout"] | Int64[Array, " rollout"]:
    """Evaluate the shared monthly harvest-yield curve, in parts per billion.

    `embedded_gain_ppb` must be clipped to `[0, PPB]` and `drawdown_ppb` must be
    non-negative. The arithmetic is integer and deliberately array-library-neutral, so the
    eager NumPy calibration helper, the traced JAX engine, and the Rust engine's
    `engine/tlh.rs` all evaluate one formula and agree exactly rather than to within a
    rounding of float64.
    """

    maturity = _pow_half_ppb(PPB - embedded_gain_ppb, maturity_decay_half_exponent)
    annual = floor_annual_yield_ppb + _mul_ppb(peak_annual_yield_ppb - floor_annual_yield_ppb, maturity)
    base_monthly = (annual + MONTHS_PER_YEAR // 2) // MONTHS_PER_YEAR
    return _mul_ppb(base_monthly, PPB + _mul_ppb(drawdown_sensitivity_ppb, drawdown_ppb))


class HarvestYieldParams(BaseModel):
    """Parameters of the reduced-form harvest-yield curve. Annual yields are gross
    realized loss as a fraction of market value; the monthly model divides by 12."""

    model_config = ConfigDict(frozen=True)

    peak_annual_yield: float = Field(
        gt=0,
        description="Gross harvested-loss yield (fraction of MV per year) at embedded_gain_fraction=0 "
        "and a neutral return — the first-year peak anchored to the TY2025 1099-B (~0.05).",
    )
    floor_annual_yield: float = Field(
        ge=0, description="Asymptotic annual yield as the account ossifies (embedded_gain_fraction -> 1)."
    )
    maturity_decay_exponent: float = Field(
        gt=0,
        description="Exponent gamma in the (1 - embedded_gain_fraction)**gamma maturity decay between "
        "floor and peak. Larger = faster decay as embedded gains build. Must be a multiple of 0.5: "
        "the curve is evaluated in exact integer arithmetic, where a half-integer power is whole "
        "multiplications and at most one square root and an arbitrary one is not.",
    )
    drawdown_sensitivity: float = Field(
        ge=0,
        description="Extra harvest per unit of negative period return: the base monthly yield is scaled "
        "by (1 + drawdown_sensitivity * max(0, -period_return)).",
    )

    @model_validator(mode="after")
    def _check_floor_below_peak(self) -> HarvestYieldParams:
        if self.floor_annual_yield > self.peak_annual_yield:
            raise ValueError(
                f"floor_annual_yield ({self.floor_annual_yield}) must not exceed "
                f"peak_annual_yield ({self.peak_annual_yield})"
            )
        return self

    @model_validator(mode="after")
    def _check_half_integer_exponent(self) -> HarvestYieldParams:
        if self.maturity_decay_exponent * 2 != int(self.maturity_decay_exponent * 2):
            raise ValueError(f"maturity_decay_exponent ({self.maturity_decay_exponent}) must be a multiple of 0.5")
        return self

    @property
    def maturity_decay_half_exponent(self) -> int:
        """Twice the exponent — the whole number the integer curve raises the base to."""

        return int(self.maturity_decay_exponent * 2)

    @property
    def peak_annual_yield_ppb(self) -> int:
        return to_ppb(self.peak_annual_yield, field="peak_annual_yield")

    @property
    def floor_annual_yield_ppb(self) -> int:
        return to_ppb(self.floor_annual_yield, field="floor_annual_yield")

    @property
    def drawdown_sensitivity_ppb(self) -> int:
        return to_ppb(self.drawdown_sensitivity, field="drawdown_sensitivity")


def monthly_harvest_fraction(
    period_return: Float64[np.ndarray, " rollout"],
    embedded_gain_fraction: Float64[np.ndarray, " rollout"],
    params: HarvestYieldParams,
) -> Float64[np.ndarray, " rollout"]:
    """Fraction of market value harvested as gross realized loss this month, per rollout.

    `period_return` and `embedded_gain_fraction` are `(R,)` arrays; the result is `(R,)`.
    The caller multiplies by market value and clamps to the loss actually available
    below basis — this function only shapes the yield curve.
    """

    # e -> [0, 1]: 0 = fresh (basis == MV, peak harvest), 1 = fully ossified (floor).
    e = np.clip(embedded_gain_fraction, 0.0, 1.0)
    # Drawdowns surface more lots below basis; up months get no kicker (drawdown == 0).
    drawdown = np.maximum(0.0, -period_return)
    fraction_ppb = harvest_fraction_curve_ppb(
        np.rint(e * PPB).astype(np.int64),
        np.rint(drawdown * PPB).astype(np.int64),
        peak_annual_yield_ppb=params.peak_annual_yield_ppb,
        floor_annual_yield_ppb=params.floor_annual_yield_ppb,
        maturity_decay_half_exponent=params.maturity_decay_half_exponent,
        drawdown_sensitivity_ppb=params.drawdown_sensitivity_ppb,
    )
    return np.asarray(fraction_ppb / PPB, dtype=np.float64)
