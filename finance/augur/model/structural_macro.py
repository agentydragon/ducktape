"""A small structural macro provider: cash, Treasuries, municipals and broad equity.

Built for one question — *how should I allocate my current assets?* — and deliberately not
for every instrument. It needs enough of the economy to choose between cash, a Treasury bond
fund, a California municipal fund and broad equity, which is close to the smallest set that
can express both a FIRE 60/40 and a floor-plus-surplus construction.

**What makes it structural is where the coupling lives.** Its latent state is a handful of
macro factors that are never emitted; its emissions are the per-instrument dollar primitives
the simulator already consumes. One rate shock therefore moves a fund's price DOWN and its
payout UP coherently, because both are derived from the same state and the same duration —
and nothing downstream learns they are related. Fitting a per-symbol price series and a
per-symbol payout series independently would put that relation outside the model, where two
aggregate bond funds could drift apart for no economic reason.

Coherently, but not proportionally, and the difference is the point: the price responds to
the rate move at once and the payout responds over years. They are two different functions of
the same state, not one series and a multiple of it.

The stochastic state is a JOINT VAR(1) on three quantities, only the last of them emitted:

- `short_rate` — prices cash and anchors the front of the curve
- `term_spread` — 10y minus short. A fund's price move is its duration times the change in
  the yield AT ITS DURATION, so a single rate cannot price a short fund and a long one
- `inflation_rate` — trailing-year, which is what a bond sleeve and a central bank both react
  to. Its INTEGRAL is the emitted CPI level

Joint rather than three separate processes, because the couplings are where the answer lives:
inflation is persistent (own lag 0.981), the short rate loads on lagged inflation with a
long-run pass-through of 1.77 — above the Taylor principle, which nothing here imposed — and
the innovations are correlated. Everything else is a deterministic function of that state plus
its own shock. An instrument is
a config ROW, not another random walk: a symbol, a duration, and a static spread over the
curve at that duration. Adding a fourth fund adds a row. A muni's spread is negative and
constant here — the cyclical part of a credit spread is a real thing this model does not
have, and the honest consequence is that it cannot produce a muni selloff that Treasuries
escape. Equity carries its own shock plus a `rate_beta` term on the short rate — the only
bond/equity channel there is, and it is fitted to ZERO, so equity and rates are in practice
INDEPENDENT here.

What the model is, what it is fitted on, and what it cannot answer — including that
independence, which is load-bearing — is declared in <SPEC.md>. Read that before trusting a
number out of this.

There is no factor concept in the public surface here, and nothing in `Sampler` asks for one:
what this model does between its state and its emissions is its own business.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Literal

import numpy as np
from pydantic import Field, NonNegativeFloat, PositiveFloat, model_validator

from finance.augur.model.exogenous import ExogenousSamplingRequest, SampledExogenousBundle, assemble_level_frames
from finance.augur.model.schemas import FrozenModel
from finance.augur.model.series import (
    InflationKey,
    IssuerId,
    LevelSeriesKey,
    SecurityDistributionKey,
    SecurityKey,
    SecuritySymbol,
)
from finance.augur.model.series_model import derive_stream_rollout_seeds

MONTHS_PER_YEAR = 12

MINIMUM_ANNUAL_YIELD = 0.0001
"""Floor on any modeled yield, as a decimal (1bp).

Not a fudge. A distribution per unit is a level series and the level stack is multiplicative,
so a zero breaks it — and a short rate that reaches zero is 2009-2021, not a hypothetical.
Real money-market funds never paid exactly zero either: a fund whose gross yield would go
negative has its fee waived instead. So the floor is both what the arithmetic needs and what
the instrument actually does.
"""


class InstrumentSpec(FrozenModel):
    """One tradable the provider prices, as a row rather than a factor.

    `duration_years` is the whole of what makes a fund respond to rates: its price moves by
    minus duration times the change in its own yield, and its payout converges toward that
    yield with a half-life of about the same number. A money-market fund is `0.0` — no price
    response and an immediate payout response, which is exactly what cash is.
    """

    symbol: SecuritySymbol
    duration_years: NonNegativeFloat
    initial_price_usd: PositiveFloat = 100.0
    # Added to the curve yield at this instrument's duration. Credit risk for a corporate
    # sleeve; NEGATIVE for municipals, which yield less than Treasuries pre-tax precisely
    # because their coupons are exempt. Tax treatment itself is the scenario's business —
    # see `SecurityDistribution.tax_character` — this is only the pre-tax price of the bond.
    spread: float = 0.0


class EquitySpec(FrozenModel):
    """Broad equity, priced as a correlated log process rather than off the curve.

    `rate_beta` is the ONLY channel to rates: the log return picks up
    `rate_beta * (change in the short rate)` on top of its own drift and shock. It exists here
    rather than in a covariance matrix because there is no covariance matrix — this model has
    structure instead — and it defaults to ZERO because the data does not support a value.
    See the field comment: the fitted coupling is the wrong sign and explains 0.4% of variance.
    """

    symbol: SecuritySymbol
    initial_price_usd: PositiveFloat
    # FITTED on VFINX adjusted close (total return), monthly, 1980-2026 (559 months):
    # 11.35%/yr nominal at 15.4% vol. VFINX rather than SPY because it is 46 years against 33
    # and covers 1987, 2000 and 2008. The hand-set 6.82% was 4.5pp low, which is why every
    # P[ruin] measured against it read high.
    #
    # Corroborated out of sample: `^GSPC` price-only over 1970-2026 gives 8.29%/yr, which with
    # a ~2.9% historical dividend yield implies ~11.2% total — and its 15.3% vol matches to a
    # tenth of a point, as it should, since dividends are smooth. That series is not used
    # directly because recovering total return from it is a modelling decision, not a data pull.
    monthly_log_return_mu: float = 0.00896
    monthly_log_return_sigma: NonNegativeFloat = 0.04433
    # ZERO, and this is a finding rather than a default. Regressing equity's monthly log return
    # on the same month's change in the short rate gives, on two windows:
    #   1993-2026 (SPY):   beta = +1.57, R^2 = 0.0041
    #   1980-2026 (VFINX): beta = -0.62, R^2 = 0.0051
    # The SIGN is not stable across windows and neither fit explains more than half a percent
    # of the variance. One window could be an unlucky sample; two that disagree on the sign are
    # the absence of a contemporaneous monthly relationship. So the model carries no coupling
    # rather than noise dressed as structure. The consequence is load-bearing and stated in
    # SPEC.md: equity and rates are INDEPENDENT here, so this model cannot answer a question
    # that turns on bond/equity correlation — which is what a 60/40 study turns on.
    rate_beta: float = 0.0


class MacroVarSpec(FrozenModel):
    """`state[t] = intercept + transition @ state[t-1] + shock_cholesky @ z[t]`, `z ~ N(0, I)`.

    State order is `(short_rate, term_spread, inflation_rate)`, every entry an annualized
    decimal. `shock_cholesky` is lower-triangular, so one draw of independent normals yields
    correctly correlated innovations — a rate surprise and an inflation surprise arrive
    together, which three separate processes cannot express at all.
    """

    initial_state: tuple[float, float, float]
    intercept: tuple[float, float, float]
    transition: tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]
    shock_cholesky: tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]

    @model_validator(mode="after")
    def _reject_explosive(self) -> MacroVarSpec:
        """A spectral radius at or above 1 is a state with no long-run mean.

        Rejected here rather than discovered at horizon 360: an explosive VAR still samples,
        and what it produces is a plausible-looking early path attached to a 30-year tail where
        the short rate reaches thousands of percent. Nothing downstream would flag that.
        """

        radius = float(np.max(np.abs(np.linalg.eigvals(np.asarray(self.transition)))))
        if radius >= 1.0:
            raise ValueError(f"macro VAR transition has spectral radius {radius:.4f} >= 1; the state is explosive")
        return self


# The fit, verbatim. Kept as a constant rather than inlined as field defaults because a VAR is
# a single object — mixing a transition row from one fit with a Cholesky factor from another
# would be silently wrong, and separate fields would let that happen one edit at a time.
FITTED_MACRO_VAR = MacroVarSpec(
    initial_state=(0.0363, 0.0084, 0.036588),
    intercept=(-0.00046861, 0.00079576, 0.00124274),
    transition=((0.989118, 0.032695, 0.019242), (-0.002894, 0.947199, -0.004301), (-0.003116, -0.038973, 0.980979)),
    shock_cholesky=((0.00479308, 0.0, 0.0), (-0.0039163, 0.00248131, 0.0), (0.00047397, 0.00051737, 0.00353245)),
)

SHORT_RATE, TERM_SPREAD, INFLATION_RATE = 0, 1, 2


class StructuralMacroProviderConfig(FrozenModel):
    """YAML config for the structural macro provider. See the module docstring."""

    type: Literal["structural_macro"] = "structural_macro"

    # --- joint macro state: a VAR(1) on (short_rate, term_spread, inflation_rate) ---------
    # FITTED by `augur.fit.structural_macro.fit_macro_var` on FRED FEDFUNDS, GS10 and
    # CPIAUCSL, monthly, 1955-08 to 2026-06 (850 months). Inflation is trailing-year log
    # inflation; all three states are annualized decimals.
    #
    # ONE process rather than three independent ones, because they are not independent and the
    # couplings are where the answer lives. The version this replaced had inflation as an iid
    # shock around a fixed drift and no link to rates at all, which produced two failures that
    # are gone here:
    #   - a 30-year price level that was effectively deterministic (1-sigma x2.64..x3.01,
    #     against x1.95..x4.85 realized). Inflation is now a STATE with its own 0.981 lag, so
    #     a high-inflation decade is reachable. Simulated 30y band: x2.11..x4.78.
    #   - no Fed reaction, so a CPI-indexed spend could outrun a bond sleeve forever. The
    #     short-rate equation now loads +0.0192 on lagged inflation, a long-run pass-through
    #     of 1.77 — above the Taylor principle's 1.0, which nothing here imposed.
    macro_state: MacroVarSpec = Field(default_factory=lambda: FITTED_MACRO_VAR)

    # The CPI level's arbitrary base. Only RATIOS of it are ever read (an amount indexed from
    # month a to month b), so the value is a unit choice; it is the inflation RATE inside
    # `macro_state` that carries the economics.
    initial_inflation_level: PositiveFloat = 100.0

    # --- instruments --------------------------------------------------------------------
    equity: EquitySpec | None = None
    instruments: tuple[InstrumentSpec, ...] = ()

    def realize_model(self) -> StructuralMacroModel:
        return StructuralMacroModel(config=self)


class StructuralMacroModel:
    """Runtime `Sampler` for `StructuralMacroProviderConfig`.

    Implements `Sampler` only. Not `Fittable`, not `Scorable`: parameters are hand-set, the
    way the independent provider's are, so a coherent rates model exists before any
    evidence-pipeline work. Fitting the rates block on its own long history (GS10 to 1953,
    FEDFUNDS to 1954) is a later and separable step — it does not go through the joint fit's
    single aligned window, which is what makes it separable.
    """

    label = "structural_macro"

    def __init__(self, config: StructuralMacroProviderConfig) -> None:
        self._config = config
        symbols = [spec.symbol for spec in config.instruments]
        if config.equity is not None:
            symbols.append(config.equity.symbol)
        # Two rows for one symbol would emit two `SecurityKey`s with the same sub-id, which
        # `assemble_level_frames` concatenates into a frame with twice the rows per rollout —
        # caught much later, by a shape check that names the symbol but not the cause.
        duplicates = sorted(symbol for symbol, count in Counter(symbols).items() if count > 1)
        if duplicates:
            raise ValueError(f"structural_macro prices a symbol more than once: {duplicates}")

    def emittable_level_keys(self) -> frozenset[LevelSeriesKey]:
        keys: set[LevelSeriesKey] = {InflationKey()}
        for spec in self._config.instruments:
            keys.add(SecurityKey(symbol=spec.symbol))
            keys.add(SecurityDistributionKey(symbol=spec.symbol))
        if self._config.equity is not None:
            # Equity emits a PRICE only. It pays dividends in reality, but `IncomeCategory`
            # has no qualified-dividend rate, so an equity distribution routed through the
            # interest path would be overtaxed as ordinary income. Emitting nothing is the
            # honest option until that third category exists.
            keys.add(SecurityKey(symbol=self._config.equity.symbol))
        return frozenset(keys)

    def emittable_private_equity_issuers(self) -> frozenset[IssuerId]:
        # This provider models public markets only. A scenario needing PE composes it with a
        # PE provider through `CompositeModel`, which is what that type is for.
        return frozenset()

    def sample(self, request: ExogenousSamplingRequest) -> SampledExogenousBundle:
        config = self._config
        rollouts = request.rollout_count
        months = request.horizon_months + 1

        state = _macro_state_path(config.macro_state, request, rollouts=rollouts, months=months)
        short_rate = np.maximum(state[SHORT_RATE], MINIMUM_ANNUAL_YIELD)
        term_spread = state[TERM_SPREAD]

        blocks: list[tuple[LevelSeriesKey, np.ndarray]] = [
            (InflationKey(), _inflation_level(config, state[INFLATION_RATE]))
        ]
        for spec in config.instruments:
            price, distribution = _instrument_paths(spec, short_rate=short_rate, term_spread=term_spread)
            blocks.append((SecurityKey(symbol=spec.symbol), price))
            blocks.append((SecurityDistributionKey(symbol=spec.symbol), distribution))
        if config.equity is not None:
            blocks.append((SecurityKey(symbol=config.equity.symbol), _equity_path(config.equity, request, short_rate)))

        return SampledExogenousBundle(
            levels=assemble_level_frames(blocks, rollout_count=rollouts, horizon_months=request.horizon_months),
            model_id=self.label,
            provenance={
                "exogenous_provider_label": self.label,
                "instruments": tuple(spec.symbol for spec in config.instruments),
                "notes": ("joint VAR(1) macro state fitted on FRED FEDFUNDS/GS10/CPIAUCSL 1955-2026",),
            },
        )


def _macro_state_path(
    spec: MacroVarSpec, request: ExogenousSamplingRequest, *, rollouts: int, months: int
) -> np.ndarray:
    """`(state, rollout, month)` — the VAR stepped forward from today's observed state.

    One shock stream for the whole block, not one per state: the innovations are CORRELATED
    (that is what `shock_cholesky` is for), so drawing them from separate seeded streams would
    silently discard the covariance the joint fit exists to capture.
    """

    intercept = np.asarray(spec.intercept)
    transition = np.asarray(spec.transition)
    cholesky = np.asarray(spec.shock_cholesky)

    normals = np.stack(
        [_shocks(request, f"structural_macro:macro_state:{index}", months=months) for index in range(len(intercept))]
    )
    path = np.empty((len(intercept), rollouts, months), dtype=np.float64)
    path[:, :, 0] = np.asarray(spec.initial_state)[:, None]
    for month in range(1, months):
        path[:, :, month] = intercept[:, None] + transition @ path[:, :, month - 1] + cholesky @ normals[:, :, month]
    return path


def _shocks(request: ExogenousSamplingRequest, stream_id: str, *, months: int) -> np.ndarray:
    """`(rollout, month)` standard normals, one independent stream per rollout.

    Seeded per (rollout, stream) exactly like every other provider, so a rollout's path
    depends only on its own seed and never on the batch it was sampled with — which is what
    lets a caller re-run one rollout of a thousand and get the same path back.
    """

    seeds = derive_stream_rollout_seeds(request.rollout_seeds, stream_id=stream_id)
    return np.stack([np.random.default_rng(seed).standard_normal(months) for seed in seeds])


def _instrument_yield(spec: InstrumentSpec, *, short_rate: np.ndarray, term_spread: np.ndarray) -> np.ndarray:
    """This instrument's yield: the curve at its duration, plus its spread.

    The curve is linear in duration between the short rate and the 10-year point. Crude, and
    adequate: the study compares a short fund, an intermediate fund and cash, and a linear
    curve orders those correctly. What it cannot do is price a barbell against a bullet.
    """

    curve_fraction = min(spec.duration_years / 10.0, 1.0)
    return np.maximum(short_rate + curve_fraction * term_spread + spec.spread, MINIMUM_ANNUAL_YIELD)


def _instrument_paths(
    spec: InstrumentSpec, *, short_rate: np.ndarray, term_spread: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """`(price, distribution_per_unit)` for one fund, both in dollars per unit.

    Price: minus `duration * change in this instrument's own yield`. No convexity term — at
    these durations it is a rounding error next to everything else that is uncertain.

    Distribution: the fund's BOOK yield over twelve, on the FACE it holds per unit — not on
    the mark. A fund pays the coupons its bonds carry, and those do not change when the bonds
    reprice; face per unit is near-constant while the mark is exactly the thing that moves.
    `initial_price_usd` stands in for that face, since a fund is issued near par.

    The book yield converges toward the market yield with a half-life of about the fund's
    duration rather than jumping to it, because a fund only earns a new yield as it rolls into
    new holdings. That lag is the structural claim the whole model exists to make, and it is
    the shape the evidence shows: BND's payout did not move in 2022 while its price fell 15%,
    then climbed from ~2.56%/yr to ~3.67%/yr across 2023-2025. Distributing on the mark would
    contradict that same evidence in the same month — it would have cut the payout 15% on the
    spot and then raised it when rates FELL, since a rate cut appreciates the mark by more
    than it erodes the book yield over any horizon shorter than the convergence.
    """

    market_yield = _instrument_yield(spec, short_rate=short_rate, term_spread=term_spread)
    months = market_yield.shape[1]

    price = np.empty_like(market_yield)
    book_yield = np.empty_like(market_yield)
    price[:, 0] = spec.initial_price_usd
    book_yield[:, 0] = market_yield[:, 0]
    # A zero-duration fund re-earns the market yield immediately; anything longer converges
    # with a half-life of its duration in months.
    convergence = (
        1.0 if spec.duration_years <= 0.0 else 1.0 - math.exp(-math.log(2.0) / (spec.duration_years * MONTHS_PER_YEAR))
    )

    for month in range(1, months):
        yield_change = market_yield[:, month] - market_yield[:, month - 1]
        # `exp(-D·Δy)` rather than `1 - D·Δy`: same first-order duration response, and it
        # cannot produce a negative price, so no arbitrary floor has to defend the positivity
        # the level stack requires. The coupon the fund earns leaves as a distribution instead
        # of compounding into the price, which is why nothing but the duration term is here.
        price[:, month] = price[:, month - 1] * np.exp(-spec.duration_years * yield_change)
        book_yield[:, month] = book_yield[:, month - 1] + convergence * (
            market_yield[:, month] - book_yield[:, month - 1]
        )

    # `book_yield` is a convex combination of market yields, each floored at 1bp, so the payout
    # is strictly positive without a floor of its own.
    return price, book_yield * spec.initial_price_usd / MONTHS_PER_YEAR


def _equity_path(spec: EquitySpec, request: ExogenousSamplingRequest, short_rate: np.ndarray) -> np.ndarray:
    """Broad equity as a log process with a rates term, so it is not independent of the curve."""

    shocks = _shocks(request, "structural_macro:equity", months=short_rate.shape[1])
    rate_changes = np.diff(short_rate, axis=1, prepend=short_rate[:, :1])
    log_returns = spec.monthly_log_return_mu + spec.monthly_log_return_sigma * shocks + spec.rate_beta * rate_changes
    # Month 0 is the anchor, not a return: every emitted series starts at its configured level.
    log_returns[:, 0] = 0.0
    return spec.initial_price_usd * np.exp(np.cumsum(log_returns, axis=1))


def _inflation_level(config: StructuralMacroProviderConfig, inflation_rate: np.ndarray) -> np.ndarray:
    """CPI level from the annualized inflation-rate STATE: one twelfth of it accrues each month.

    Always positive, because it is an exponential of a sum — so deflation is representable
    (a negative rate) without the level ever reaching zero, which the multiplicative level
    stack requires. The previous version drew an i.i.d. log return each month and so had no
    persistence at all; here the persistence lives in the state and this is just its integral.
    """

    monthly = inflation_rate / MONTHS_PER_YEAR
    # Month 0 is the anchor, not a return: the level starts at exactly the configured value.
    monthly[:, 0] = 0.0
    return config.initial_inflation_level * np.exp(np.cumsum(monthly, axis=1))
