"""Fit `structural_macro`'s checked-in defaults: the joint macro VAR (`macro_var.fit_macro_var`)
and the equity log-return / rate-beta blocks (`equity.fit_log_returns`, `equity.fit_rate_beta`),
each on its OWN longest window rather than one window shared by all of them.

That is the payoff of a structural model over a covariance matrix, and it is why this provider
carries no crypto and needs no factor block: the joint VECM/state-space fit inner-joins every
series into ONE aligned window, so adding a 1954 series there would not buy 1954 — it would be
truncated to whatever the shortest series allows (ETH, ~2017). Nothing here shares a window
with anything it is not correlated to: `FEDFUNDS` (1954-07) and `GS10` (1953-04) feed the
macro VAR and nothing else, and 70 years is exactly what makes that fit worth doing.

The rule, stated so it can be argued with: a MARGINAL (a drift, a volatility, the macro VAR
itself) is fitted on its own longest history; a CROSS-BLOCK parameter must use the common
window, because a covariance is undefined where the series do not overlap. `fit_rate_beta` is
the only cross-block parameter in the model, so it is the only thing paying the truncation.

The hazard this leaves, named rather than hidden: inflation reaches back to 1947 and equity
only to 1993, so the model's implied REAL equity return pairs a sample containing the 1970s
with one that does not. It lands at ~7.3%/yr real, close to the long-run realized figure, so
the mismatch is not currently doing damage — but it is a coincidence, not a control.

See `ornstein_uhlenbeck.py` for a simpler, independent-rates alternative fit that predates this
and is not currently wired in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path

import polars as pl

from finance.augur.model.historical_windows import splice_monthly_frames
from finance.augur.x.models.equity_fit import fit_log_returns, fit_rate_beta
from finance.augur.x.models.macro_var import fit_macro_var
from finance.augur.x.models.structural_macro import (
    PERCENT_TO_DECIMAL,
    FitWindowProvenance,
    MacroVarSpec,
    StructuralMacroFittedDefaults,
)
from finance.evidence import loading, sources
from finance.evidence.loading import MonthlyLevel
from finance.evidence.snapshot import EvidenceSnapshot
from finance.evidence.sources import FRED_CPI, FRED_FEDFUNDS, FRED_GS10, FRENCH_FACTORS, YAHOO_VFINX

LONG_RECORD_START = date(1926, 7, 1)
"""The first month in the French factor record used by the long-record fit."""

LONG_RECORD_MACRO_SOURCES = (FRENCH_FACTORS, sources.FRED_LTGOVTBD, FRED_GS10, sources.FRED_CPI_NSA)
"""The three input series for the long-record VAR and the sources needed to construct them."""


class MacroFitWindow(StrEnum):
    """Which record the joint macro VAR is estimated on.

    Required rather than defaulted. Both are defensible and they are not the same estimate, so
    a caller that did not choose would be making a modelling decision by omission — and the
    choice would appear neither at the call site nor in any report of the fit.
    """

    FRED_1955 = "fred_1955"
    """FRED `FEDFUNDS` / `GS10` / `CPIAUCSL`, 1955-08 on. 850 months, all three measured
    directly, no splice."""

    LONG_RECORD_1926 = "long_record_1926"
    """The long-record short rate, spliced long rate, and NSA CPI, 1926-07 on. Its end is the
    latest shared month among those three inputs; corporate-yield and equity sources no longer
    truncate this fit. About 41% more months than the FRED window, and it
    reaches the Depression, the 1940s inflation and the WWII rate peg — the clustered bad
    decades a CPI-indexed spender is most exposed to.

    Not simply more of the same series, and the differences are the reason this is a choice
    rather than an upgrade: the short rate is Ken French's one-month T-bill rather than the fed
    funds rate, the long rate is `FRED_LTGOVTBD` spliced into `GS10` (the one step
    `load_macro_history` calls unquantified in its error), and CPI is the NSA series. It also
    pools the pre-1951 rate peg, a policy regime that no longer exists, into one stationary
    process — which may make the rate block worse rather than better.
    """


@dataclass(frozen=True)
class MacroVarLevels:
    """The three percent series `fit_macro_var` reads, grouped so they travel together."""

    short_rate_percent: list[MonthlyLevel]
    long_rate_percent: list[MonthlyLevel]
    cpi_level: list[MonthlyLevel]


def _macro_var_levels(frames: dict[str, pl.DataFrame], *, start: date | None = None) -> MacroVarLevels:
    expected = {"short_rate_percent", "long_rate_percent", "cpi_level"}
    if set(frames) != expected:
        raise ValueError(f"macro VAR needs exactly these selected series: {sorted(expected)}")

    # Keep each source's own calendar: the VAR state needs CPI from a year before each
    # state month even when rate data were missing at that earlier date.
    selected = {}
    for name, frame in frames.items():
        validated = loading.align_monthly_frames({name: frame}, value_column="value").rename({name: "value"})
        if start is not None:
            validated = validated.filter(pl.col("month") >= start)
            if validated.is_empty() or validated.get_column("month")[0] != start:
                raise ValueError(f"long-record macro input {name} does not cover the required start month {start}")
        selected[name] = loading.monthly_levels(validated)
    return MacroVarLevels(
        short_rate_percent=selected["short_rate_percent"],
        long_rate_percent=selected["long_rate_percent"],
        cpi_level=selected["cpi_level"],
    )


def _long_record_macro_var_frames(cache: loading.EvidenceFrameCache) -> dict[str, pl.DataFrame]:
    factors = cache.frame(FRENCH_FACTORS)
    short_rate = factors.select("month", (pl.col("risk_free_rate") * 12.0 * 100.0).alias("value"))
    long_rate = splice_monthly_frames(
        early=cache.monthly_frame(sources.FRED_LTGOVTBD), late=cache.monthly_frame(FRED_GS10)
    )
    return {
        "short_rate_percent": short_rate,
        "long_rate_percent": long_rate,
        "cpi_level": cache.monthly_frame(sources.FRED_CPI_NSA),
    }


def load_long_record_macro_var_levels(evidence_dir: Path) -> MacroVarLevels:
    """Load the three independently dated series used by the 1926 long-record VAR."""
    return _macro_var_levels(
        _long_record_macro_var_frames(
            loading.EvidenceFrameCache(EvidenceSnapshot.capture(evidence_dir, LONG_RECORD_MACRO_SOURCES))
        ),
        start=LONG_RECORD_START,
    )


def fit_structural_macro_defaults(evidence_dir: Path, *, macro_window: MacroFitWindow) -> StructuralMacroFittedDefaults:
    """Fit `structural_macro`'s checked-in defaults from real evidence — see the module
    docstring for why these are three separable fits rather than one joint window.
    """
    macro_sources = (
        (FRED_FEDFUNDS, FRED_GS10, FRED_CPI) if macro_window == MacroFitWindow.FRED_1955 else LONG_RECORD_MACRO_SOURCES
    )
    fit_sources = tuple(dict.fromkeys((*macro_sources, FRENCH_FACTORS, YAHOO_VFINX, FRED_FEDFUNDS)))
    snapshot = EvidenceSnapshot.capture(evidence_dir, fit_sources)
    cache = loading.EvidenceFrameCache(snapshot)
    match macro_window:
        case MacroFitWindow.FRED_1955:
            levels = _macro_var_levels(
                {
                    "short_rate_percent": cache.monthly_frame(FRED_FEDFUNDS),
                    "long_rate_percent": cache.monthly_frame(FRED_GS10),
                    "cpi_level": cache.monthly_frame(FRED_CPI),
                }
            )
            macro_fit = fit_macro_var(
                short_rate_percent=levels.short_rate_percent,
                long_rate_percent=levels.long_rate_percent,
                cpi_level=levels.cpi_level,
            )
            macro_source = f"{FRED_FEDFUNDS.provenance_label},{FRED_GS10.provenance_label},{FRED_CPI.provenance_label}"
        case MacroFitWindow.LONG_RECORD_1926:
            levels = _macro_var_levels(_long_record_macro_var_frames(cache), start=LONG_RECORD_START)
            macro_fit = fit_macro_var(
                short_rate_percent=levels.short_rate_percent,
                long_rate_percent=levels.long_rate_percent,
                cpi_level=levels.cpi_level,
            )
            macro_source = ",".join(source.provenance_label for source in LONG_RECORD_MACRO_SOURCES)
    factors = cache.frame(FRENCH_FACTORS)
    equity = loading.compound_monthly_returns(factors, return_column="market_total_return")
    equity_fit = fit_log_returns(loading.monthly_levels(equity))
    beta = loading.align_monthly_frames(
        {
            "equity": cache.monthly_frame(YAHOO_VFINX),
            "short_rate": cache.monthly_frame(FRED_FEDFUNDS).with_columns(
                (pl.col("value") * PERCENT_TO_DECIMAL).alias("value")
            ),
        },
        value_column="value",
    )
    beta_fit = fit_rate_beta(
        equity_levels=loading.monthly_levels(beta.select("month", pl.col("equity").alias("value"))),
        short_rate=loading.monthly_levels(beta.select("month", pl.col("short_rate").alias("value"))),
    )

    return StructuralMacroFittedDefaults(
        evidence_revision=snapshot.revision,
        evidence_source_sha256=snapshot.source_hashes(),
        macro_state=MacroVarSpec(
            initial_state=macro_fit.latest_state,
            intercept=macro_fit.intercept,
            transition=macro_fit.transition,
            shock_cholesky=macro_fit.shock_cholesky,
        ),
        macro_state_fit=FitWindowProvenance(
            source=macro_source,
            first_month=macro_fit.first_month,
            last_month=macro_fit.latest_month,
            sample_months=macro_fit.sample_months,
        ),
        equity_monthly_log_return_mu=equity_fit.monthly_log_mu,
        equity_monthly_log_return_sigma=equity_fit.monthly_log_sigma,
        equity_fit=FitWindowProvenance(
            source=FRENCH_FACTORS.provenance_label,
            first_month=equity_fit.first_month,
            last_month=equity_fit.last_month,
            sample_months=equity_fit.sample_months,
        ),
        rate_beta_fit=FitWindowProvenance(
            source=f"{YAHOO_VFINX.provenance_label},{FRED_FEDFUNDS.provenance_label}",
            first_month=beta_fit.first_month,
            last_month=beta_fit.last_month,
            sample_months=beta_fit.sample_months,
        ),
        rate_beta_fitted_value=beta_fit.beta,
        rate_beta_r_squared=beta_fit.r_squared,
    )
