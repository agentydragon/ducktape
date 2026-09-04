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

from pathlib import Path

from finance.augur.fit.equity import fit_log_returns, fit_rate_beta
from finance.augur.fit.macro_var import fit_macro_var
from finance.augur.model.structural_macro import (
    PERCENT_TO_DECIMAL,
    FitWindowProvenance,
    MacroVarSpec,
    StructuralMacroFittedDefaults,
)
from finance.evidence.loading import MonthlyLevel, read_french_market_levels, read_monthly_levels
from finance.evidence.sources import FRED_CPI, FRED_FEDFUNDS, FRED_GS10, FRENCH_FACTORS, YAHOO_VFINX


def fit_structural_macro_defaults(evidence_dir: Path) -> StructuralMacroFittedDefaults:
    """Fit `structural_macro`'s checked-in defaults from real evidence — see the module
    docstring for why these are three separable fits rather than one joint window.
    """
    fedfunds_percent = read_monthly_levels(evidence_dir, FRED_FEDFUNDS)
    macro_fit = fit_macro_var(
        short_rate_percent=fedfunds_percent,
        long_rate_percent=read_monthly_levels(evidence_dir, FRED_GS10),
        cpi_level=read_monthly_levels(evidence_dir, FRED_CPI),
    )
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
            source=f"{FRED_FEDFUNDS.provenance_label},{FRED_GS10.provenance_label},{FRED_CPI.provenance_label}",
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
