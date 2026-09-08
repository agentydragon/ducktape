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

from collections import Counter
from datetime import date
from typing import Literal

import numpy as np
import yaml
from pydantic import Field, NonNegativeFloat, PositiveFloat, model_validator

from finance.augur.model.bond_fund import YieldCurve, constant_maturity_fund_paths
from finance.augur.model.exogenous import ExogenousSamplingRequest, SampledExogenousBundle, assemble_level_frames
from finance.augur.model.float64 import LEVEL_DTYPE
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
from util.bazel.runfiles import get_required_path, own_repo_rlocation

MONTHS_PER_YEAR = 12

# FRED publishes both rate series in PERCENT; every rate inside augur is a decimal.
PERCENT_TO_DECIMAL = 0.01

# A fit needs enough months that its estimate is not noise. 240 (20 years) is well below the
# ~865 the real rate series carry and well above anything that could fit a single regime.
MINIMUM_MONTHS = 240

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

    A constant-maturity bond fund: it holds a par bond of `maturity_years`, collects its
    coupon, and each month rolls into a fresh one. `bond_fund.constant_maturity_fund_paths`
    is the arithmetic, and duration is an OUTPUT of it — a longer maturity moves more for the
    same yield change, without duration being a parameter anyone sets. A money-market fund is
    a maturity near zero: no price response and an immediate payout response, which is what
    cash is.
    """

    symbol: SecuritySymbol
    # Maturity, not duration: the bond math needs a maturity to discount to, and it yields the
    # duration response rather than taking one.
    maturity_years: NonNegativeFloat
    initial_price_usd: PositiveFloat = 100.0
    # Which observed yield this fund earns. A corporate sleeve names its own curve rather than
    # taking a guessed credit spread over governments.
    yield_curve: YieldCurve = YieldCurve.GOVERNMENT
    # Two ways to sit off the curve, because they are different economics and a muni needs the
    # second. `spread` is ADDITIVE, which is what a credit spread is: a corporate yields the
    # government curve plus compensation for default risk, roughly independent of the level.
    # `curve_ratio` is MULTIPLICATIVE, which is what a tax exemption is: a muni yields a
    # FRACTION of the taxable curve, anchored near `1 - t` for the marginal investor, so the gap
    # narrows as rates fall instead of staying put. An additive muni spread is a linearisation
    # of that ratio around current rates, and it goes NEGATIVE once the curve drops below the
    # spread — see #5832, where the clamp that catches it then becomes a coupon and collapses
    # the fund.
    #
    # Both are static here, which is the simplification that remains: the ratio has no dynamics
    # of its own, so this still cannot produce a muni selloff Treasuries escape (gap 5). #5835
    # gives munis their own factor, fitted jointly, and #5834 replaces the two-point curve these
    # sit on. Tax treatment itself stays the scenario's business — see
    # `SecurityDistribution.tax_character`; this is only the pre-tax price.
    spread: float = 0.0
    curve_ratio: PositiveFloat = 1.0


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
    # Defaults to the checked-in fit on the CRSP value-weighted total US market (Ken French's
    # factors, `Mkt-RF + RF`, dividends included) — see `fit/calibrated/trained_structural_macro
    # .yaml`'s `equity_fit` for the window/sample count, and SPEC.md gap 3 for why a century
    # rather than a shorter window is the deliberate choice.
    monthly_log_return_mu: float = Field(default_factory=lambda: _fitted_defaults().equity_monthly_log_return_mu)
    monthly_log_return_sigma: NonNegativeFloat = Field(
        default_factory=lambda: _fitted_defaults().equity_monthly_log_return_sigma
    )
    # ZERO by POLICY, not by fit — the fitted coupling's sign is not stable across windows and
    # neither window explains half a percent of variance (SPEC.md gap 2 has the numbers; the
    # checked-in fit's `rate_beta_fit`/`rate_beta_fitted_value`/`rate_beta_r_squared` are the
    # same finding as data). So the model carries no bond/equity coupling rather than noise
    # dressed as structure — load-bearing per SPEC.md: a question that turns on bond/equity
    # correlation is not answered here.
    rate_beta: float = 0.0


type MacroStateVector = tuple[float, float, float]
type MacroStateMatrix = tuple[MacroStateVector, MacroStateVector, MacroStateVector]


class MacroVarSpec(FrozenModel):
    """`state[t] = intercept + transition @ state[t-1] + shock_cholesky @ z[t]`, `z ~ N(0, I)`.

    State order is `(short_rate, term_spread, inflation_rate)`, every entry an annualized
    decimal. `shock_cholesky` is lower-triangular, so one draw of independent normals yields
    correctly correlated innovations — a rate surprise and an inflation surprise arrive
    together, which three separate processes cannot express at all.
    """

    initial_state: MacroStateVector
    intercept: MacroStateVector
    transition: MacroStateMatrix
    shock_cholesky: MacroStateMatrix

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


class FitWindowProvenance(FrozenModel):
    """A fitted block's evidence window, checked in as data rather than left to a comment
    beside the numbers: which series it came from, the window it was fitted on, and how many
    months that window covered."""

    source: str
    first_month: date
    last_month: date
    sample_months: int


class StructuralMacroFittedDefaults(FrozenModel):
    """The structural-macro fit, checked in whole: written by `bb run
    //finance/augur/fit:train -- --model structural_macro ...` to
    `fit/calibrated/trained_structural_macro.yaml` and loaded (via `_fitted_defaults` below)
    as `StructuralMacroProviderConfig`'s and `EquitySpec`'s shipped defaults.

    Deployment-specific fields are deliberately absent — which equity symbol and which
    instruments a scenario prices are not fit outputs, so they stay on
    `StructuralMacroProviderConfig`/`InstrumentSpec`, supplied per scenario.
    """

    macro_state: MacroVarSpec
    macro_state_fit: FitWindowProvenance

    equity_monthly_log_return_mu: float
    equity_monthly_log_return_sigma: NonNegativeFloat
    equity_fit: FitWindowProvenance

    # `EquitySpec.rate_beta` stays a policy-set 0.0 rather than this fitted value — see its
    # field comment. Recorded here so that policy is checkable against real evidence instead
    # of asserted, and so a future refit's rate_beta finding is a reviewable diff.
    rate_beta_fit: FitWindowProvenance
    rate_beta_fitted_value: float
    rate_beta_r_squared: float


# Repository-relative location of the checked-in fit — see `StructuralMacroFittedDefaults`.
# Resolved through `own_repo_rlocation` rather than hardcoding `_main`: this is library code,
# and a dependent repo importing it is `_main` itself.
_BUNDLED_STRUCTURAL_MACRO_RUNFILE = "finance/augur/fit/calibrated/trained_structural_macro.yaml"


def _fitted_defaults() -> StructuralMacroFittedDefaults:
    path = get_required_path(own_repo_rlocation(_BUNDLED_STRUCTURAL_MACRO_RUNFILE))
    return StructuralMacroFittedDefaults.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


SHORT_RATE, TERM_SPREAD, INFLATION_RATE = 0, 1, 2


class StructuralMacroProviderConfig(FrozenModel):
    """YAML config for the structural macro provider. See the module docstring."""

    type: Literal["structural_macro"] = "structural_macro"

    # --- joint macro state: a VAR(1) on (short_rate, term_spread, inflation_rate) ---------
    # Defaults to the checked-in fit (`fit/calibrated/trained_structural_macro.yaml`'s
    # `macro_state` + `macro_state_fit` for the window/sample count) rather than a module
    # constant, so a refit is a reviewable diff instead of a hand-transcription. Inflation is
    # trailing-year log inflation; all three states are annualized decimals. Why one JOINT
    # process rather than three independent ones — persistence, the Fed's inflation reaction,
    # correlated innovations — is SPEC.md's "What is fitted, and on what" and gap 1.
    macro_state: MacroVarSpec = Field(default_factory=lambda: _fitted_defaults().macro_state)

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

    Implements `Sampler` only. Not `Fittable`, not `Scorable`: fitting happens offline
    (`fit/structural_macro.py`'s `fit_structural_macro_defaults`, run via `bb run
    //finance/augur/fit:train -- --model structural_macro`), against each block's own
    longest window rather than the `Fittable` protocol's shared aligned `HistoricalSeries` —
    which is the whole reason this provider's state is two rates rather than a factor block.
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
            market_yield = _instrument_yield(spec, short_rate=short_rate, term_spread=term_spread)
            price, distribution = constant_maturity_fund_paths(
                market_yield, maturity_years=spec.maturity_years, initial_price_usd=spec.initial_price_usd
            )
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
    path = np.empty((len(intercept), rollouts, months), dtype=LEVEL_DTYPE)
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
    """This instrument's yield: its share of the curve at its maturity, plus its spread.

    The curve is linear in maturity between the short rate and the 10-year point, and flat past
    ten years. Crude, and adequate for ordering cash against a short and an intermediate fund;
    it cannot price a barbell against a bullet, and it misprices a long ladder badly. #5834
    replaces it with a fitted curve.

    `curve_ratio` scales the curve BEFORE `spread` is added, so a muni's exemption stays
    proportional to the level while a credit spread stays absolute. That ordering is what keeps
    the yield off the floor as rates fall: a ratio of a small number is small, a constant
    subtracted from a small number is negative.
    """

    if spec.yield_curve is not YieldCurve.GOVERNMENT:
        raise ValueError(
            f"{spec.symbol} prices off {spec.yield_curve}, which this model cannot produce: its state is "
            "(short rate, term spread, inflation) with no credit factor, so a corporate yield here would be "
            "a government yield plus a constant — which is what the instrument model was replaced for. "
            "Use the historical-windows provider, whose record carries Moody's Aaa and Baa, or add a credit "
            "factor to the VAR."
        )
    curve_fraction = min(spec.maturity_years / 10.0, 1.0)
    curve = short_rate + curve_fraction * term_spread
    return np.maximum(spec.curve_ratio * curve + spec.spread, MINIMUM_ANNUAL_YIELD)


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
