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
from enum import StrEnum
from pathlib import Path

from finance.augur.fit.equity import fit_log_returns, fit_rate_beta
from finance.augur.fit.macro_var import fit_macro_var
from finance.augur.model.historical_windows import (
    DECIMAL_TO_PERCENT,
    MACRO_HISTORY_SOURCES,
    MacroHistory,
    load_macro_history,
)
from finance.augur.model.structural_macro import (
    PERCENT_TO_DECIMAL,
    FitWindowProvenance,
    MacroVarSpec,
    StructuralMacroFittedDefaults,
)
from finance.evidence.loading import MonthlyLevel, read_french_market_levels, read_monthly_levels
from finance.evidence.sources import FRED_CPI, FRED_FEDFUNDS, FRED_GS10, FRENCH_FACTORS, YAHOO_VFINX


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
    """The record `load_macro_history` assembles, 1926-07 on. About 41% more months, and it
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


def macro_var_levels(history: MacroHistory) -> MacroVarLevels:
    """`MacroHistory`'s aligned arrays as the percent series `fit_macro_var` reads.

    `MacroHistory` carries annualized DECIMALS and a term spread; `fit_macro_var` takes percent
    and a long rate, so the long rate is reconstituted as short + spread. Going through the
    assembled record rather than re-reading the sources is the point: one assembly path, so the
    fit and the replay sampler cannot come to disagree about what the century was.
    """

    return MacroVarLevels(
        short_rate_percent=[
            MonthlyLevel(month=month, value=rate * DECIMAL_TO_PERCENT)
            for month, rate in zip(history.months, history.short_rate.tolist(), strict=True)
        ],
        long_rate_percent=[
            MonthlyLevel(month=month, value=(short + spread) * DECIMAL_TO_PERCENT)
            for month, short, spread in zip(
                history.months, history.short_rate.tolist(), history.term_spread.tolist(), strict=True
            )
        ],
        cpi_level=[
            MonthlyLevel(month=month, value=level)
            for month, level in zip(history.months, history.cpi_level.tolist(), strict=True)
        ],
    )


def fit_structural_macro_defaults(evidence_dir: Path, *, macro_window: MacroFitWindow) -> StructuralMacroFittedDefaults:
    """Fit `structural_macro`'s checked-in defaults from real evidence — see the module
    docstring for why these are three separable fits rather than one joint window.
    """
    fedfunds_percent = read_monthly_levels(evidence_dir, FRED_FEDFUNDS)
    match macro_window:
        case MacroFitWindow.FRED_1955:
            macro_fit = fit_macro_var(
                short_rate_percent=fedfunds_percent,
                long_rate_percent=read_monthly_levels(evidence_dir, FRED_GS10),
                cpi_level=read_monthly_levels(evidence_dir, FRED_CPI),
            )
            macro_source = f"{FRED_FEDFUNDS.provenance_label},{FRED_GS10.provenance_label},{FRED_CPI.provenance_label}"
        case MacroFitWindow.LONG_RECORD_1926:
            levels = macro_var_levels(load_macro_history(evidence_dir))
            macro_fit = fit_macro_var(
                short_rate_percent=levels.short_rate_percent,
                long_rate_percent=levels.long_rate_percent,
                cpi_level=levels.cpi_level,
            )
            macro_source = ",".join(source.provenance_label for source in MACRO_HISTORY_SOURCES)
    equity_fit = fit_log_returns(read_french_market_levels(evidence_dir, FRENCH_FACTORS))
    beta_fit = fit_rate_beta(
        equity_levels=read_monthly_levels(evidence_dir, YAHOO_VFINX),
        # fit_rate_beta takes a decimal-scale rate; FRED's FEDFUNDS is percent.
        short_rate=[
            MonthlyLevel(month=level.month, value=level.value * PERCENT_TO_DECIMAL) for level in fedfunds_percent
        ],
    )

    return StructuralMacroFittedDefaults(
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
