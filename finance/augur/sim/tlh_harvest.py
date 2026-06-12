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

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
from pydantic import BaseModel, ConfigDict, Field, model_validator

_MONTHS_PER_YEAR = 12.0


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
        "floor and peak. Larger = faster decay as embedded gains build.",
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


@dataclass(frozen=True)
class HarvestSplit:
    """Gross harvested loss split by holding period, per rollout (USD, non-negative)."""

    short_term_usd: npt.NDArray[np.float64]
    long_term_usd: npt.NDArray[np.float64]


def monthly_harvest_fraction(
    period_return: npt.NDArray[np.float64], embedded_gain_fraction: npt.NDArray[np.float64], params: HarvestYieldParams
) -> npt.NDArray[np.float64]:
    """Fraction of market value harvested as gross realized loss this month, per rollout.

    `period_return` and `embedded_gain_fraction` are `(R,)` arrays; the result is `(R,)`.
    The caller multiplies by market value and clamps to the loss actually available
    below basis — this function only shapes the yield curve.
    """

    # e -> [0, 1]: 0 = fresh (basis == MV, peak harvest), 1 = fully ossified (floor).
    e = np.clip(embedded_gain_fraction, 0.0, 1.0)
    # Front-loaded decay: (1 - e)**gamma falls from 1 (fresh) to 0 (ossified) [VANGUARD-2024].
    maturity = (1.0 - e) ** params.maturity_decay_exponent
    base_monthly = (
        params.floor_annual_yield + (params.peak_annual_yield - params.floor_annual_yield) * maturity
    ) / _MONTHS_PER_YEAR
    # Drawdowns surface more lots below basis; up months get no kicker (drawdown == 0).
    drawdown = np.maximum(0.0, -period_return)
    return base_monthly * (1.0 + params.drawdown_sensitivity * drawdown)


def split_short_long(
    gross_harvest_usd: npt.NDArray[np.float64], short_term_fraction: npt.NDArray[np.float64]
) -> HarvestSplit:
    """Split gross harvested loss into ST/LT by the harvestable short-term share.

    `short_term_fraction` (per rollout, in `[0, 1]`) is seeded from the holding's
    holding-period buckets — near 1.0 for a young account (mostly short-term losses).
    """

    stf = np.clip(short_term_fraction, 0.0, 1.0)
    return HarvestSplit(short_term_usd=gross_harvest_usd * stf, long_term_usd=gross_harvest_usd * (1.0 - stf))
