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

The stochastic state is two rates, neither of them emitted:

- `short_rate` — prices cash and anchors the front of the curve
- `term_spread` — 10y minus short. A fund's price move is its duration times the change in
  the yield AT ITS DURATION, so a single rate cannot price a short fund and a long one

Everything else is a deterministic function of those two plus its own shock. An instrument is
a config ROW, not another random walk: a symbol, a duration, and a static spread over the
curve at that duration. Adding a fourth fund adds a row. A muni's spread is negative and
constant here — the cyclical part of a credit spread is a real thing this model does not
have, and the honest consequence is that it cannot produce a muni selloff that Treasuries
escape. Equity carries its own shock plus a `rate_beta` term on the short rate, which is the
only bond/equity coupling there is; inflation carries its own shock and is the one series
that is both state and emission, since spending is CPI-indexed.

There is no factor concept in the public surface here, and nothing in `Sampler` asks for one:
what this model does between its state and its emissions is its own business.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Literal

import numpy as np
from pydantic import Field, NonNegativeFloat, PositiveFloat

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

    `rate_beta` is the whole coupling to rates: the log return picks up
    `rate_beta * (change in the short rate)` on top of its own drift and shock. Negative by
    convention — a rate rise is an equity headwind — and small, because the relationship is
    real but weak. It is here rather than in a covariance matrix because there is no
    covariance matrix: this model has structure instead.
    """

    symbol: SecuritySymbol
    initial_price_usd: PositiveFloat
    monthly_log_return_mu: float = 0.0055
    monthly_log_return_sigma: NonNegativeFloat = 0.042
    rate_beta: float = -2.0


class StructuralMacroProviderConfig(FrozenModel):
    """YAML config for the structural macro provider. See the module docstring."""

    type: Literal["structural_macro"] = "structural_macro"

    # --- latent rates state -------------------------------------------------------------
    # FITTED by `augur.fit.structural_macro` on FRED FEDFUNDS and GS10 - FEDFUNDS, monthly,
    # 1954-07 to 2026-07 (865 months). The initial levels are the last observation, not fitted.
    #
    # The first draft of these was hand-set, and the fit moved two of them a long way in the
    # same direction — both toward MORE rate risk, so every P[ruin] computed against the
    # hand-set block was too kind to bonds:
    #   short-rate reversion  0.03    -> 0.0098  (a ~2-year half-life was really ~6)
    #   short-rate sigma      0.0025  -> 0.0048  (half the real monthly volatility)
    # The hand-set comment claimed 0.03 was "roughly how long a hiking or cutting cycle takes
    # to play out", which was the error: the rate does not revert on the cycle's timescale, it
    # wanders for years. The spread parameters were closer, and its sigma was also ~3x light.
    #
    # Read the sigmas; treat the means and half-lives as soft. OLS on a near-unit-root series
    # biases reversion up and barely identifies the mean at all — over 1990-2026 the same fit
    # gives a 1.71% short-rate mean against this 4.93%. `fit/structural_macro.py` documents
    # why, and a study whose answer turns on the mean should sweep it.
    initial_short_rate: NonNegativeFloat = 0.0363
    short_rate_mean: NonNegativeFloat = 0.0493
    short_rate_mean_reversion: float = Field(default=0.0098, ge=0.0, le=1.0)
    short_rate_monthly_sigma: NonNegativeFloat = 0.0048

    initial_term_spread: float = 0.0097
    term_spread_mean: float = 0.0096
    term_spread_mean_reversion: float = Field(default=0.0455, ge=0.0, le=1.0)
    term_spread_monthly_sigma: NonNegativeFloat = 0.0046

    # --- inflation ----------------------------------------------------------------------
    initial_inflation_level: PositiveFloat = 100.0
    inflation_monthly_log_mu: float = 0.0021
    inflation_monthly_log_sigma: NonNegativeFloat = 0.0025

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

        short_rate = _mean_reverting_path(
            initial=config.initial_short_rate,
            mean=config.short_rate_mean,
            reversion=config.short_rate_mean_reversion,
            sigma=config.short_rate_monthly_sigma,
            shocks=_shocks(request, "structural_macro:short_rate", months=months),
            floor=MINIMUM_ANNUAL_YIELD,
        )
        term_spread = _mean_reverting_path(
            initial=config.initial_term_spread,
            mean=config.term_spread_mean,
            reversion=config.term_spread_mean_reversion,
            sigma=config.term_spread_monthly_sigma,
            shocks=_shocks(request, "structural_macro:term_spread", months=months),
            floor=None,
        )

        blocks: list[tuple[LevelSeriesKey, np.ndarray]] = [(InflationKey(), _inflation_path(config, request, months))]
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
                "notes": ("hand-parameterised structural macro model; rates state is not fitted",),
            },
        )


def _shocks(request: ExogenousSamplingRequest, stream_id: str, *, months: int) -> np.ndarray:
    """`(rollout, month)` standard normals, one independent stream per rollout.

    Seeded per (rollout, stream) exactly like every other provider, so a rollout's path
    depends only on its own seed and never on the batch it was sampled with — which is what
    lets a caller re-run one rollout of a thousand and get the same path back.
    """

    seeds = derive_stream_rollout_seeds(request.rollout_seeds, stream_id=stream_id)
    return np.stack([np.random.default_rng(seed).standard_normal(months) for seed in seeds])


def _mean_reverting_path(
    *, initial: float, mean: float, reversion: float, sigma: float, shocks: np.ndarray, floor: float | None
) -> np.ndarray:
    """Ornstein-Uhlenbeck in discrete monthly steps, on the LEVEL of a rate.

    On the level, not the log: a rate legitimately reaches zero and can even go negative,
    which is precisely why a rate cannot be a `LevelSeriesKey` and only its dollar
    consequences cross the boundary. `floor` applies where a negative would be nonsense (a
    yield); the term spread has none, because an inverted curve is a real and important state.
    """

    rollouts, months = shocks.shape
    path = np.empty((rollouts, months), dtype=np.float64)
    path[:, 0] = initial
    for month in range(1, months):
        drift = reversion * (mean - path[:, month - 1])
        path[:, month] = path[:, month - 1] + drift + sigma * shocks[:, month]
    if floor is not None:
        path = np.maximum(path, floor)
    return path


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


def _inflation_path(
    config: StructuralMacroProviderConfig, request: ExogenousSamplingRequest, months: int
) -> np.ndarray:
    shocks = _shocks(request, "structural_macro:inflation", months=months)
    log_returns = config.inflation_monthly_log_mu + config.inflation_monthly_log_sigma * shocks
    log_returns[:, 0] = 0.0
    return config.initial_inflation_level * np.exp(np.cumsum(log_returns, axis=1))
