"""Score the 1955 FRED window against the 1926 long record for the joint macro VAR.

#5509 asks whether fitting on the longer record is better, not to assume it is. The added span
carries the Depression, the 1940s inflation and the WWII rate peg — the clustered bad decades a
CPI-indexed spender is most exposed to — but it is also the part least like the present, and
pre-1951 rates were PEGGED, a policy regime that no longer exists. Pooling that into one
stationary process may make the rate block worse rather than better.

So this reports the two fits side by side and lets the numbers decide. What it compares:

- **Span and sample size**, since the whole premise is +41% observations.
- **Persistence**, as the spectral radius of `A`. A VAR(1) is stationary only when that is
  below 1, and how close it sits to 1 is how long a decade of high inflation can last.
- **Shock scale**, as each state's own innovation standard deviation off the diagonal of `L`.
- **The implied stationary standard deviation** of each state, which is what persistence and
  shock scale jointly produce and what the simulator actually samples from.

The two fits do NOT read the same series, and that is the finding as much as the numbers are:
the long record's short rate is Ken French's one-month T-bill rather than the fed funds rate,
its long rate is `FRED_LTGOVTBD` spliced into `GS10`, and its CPI is the NSA series. A
difference here is a difference in window AND in measurement, and nothing below separates them.
`holdout.py` does separate them, by scoring both spans of the SAME series out of sample.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

import numpy as np

from finance.augur.fit.macro_var import MacroVarFit
from finance.augur.fit.structural_macro import MacroFitWindow, fit_structural_macro_defaults
from finance.augur.model.historical_windows import MACRO_HISTORY_SOURCES
from finance.augur.model.structural_macro import StructuralMacroFittedDefaults
from finance.augur.study.trinity.evidence_snapshot import snapshot_evidence
from finance.evidence import sources

logger = logging.getLogger(__name__)

STATE_NAMES = ("short_rate", "term_spread", "inflation")

# Everything `fit_structural_macro_defaults` reads on EITHER window, so one fetch serves both
# fits. It fits three blocks, not just the macro VAR: the equity marginal and the rate beta run
# whichever window is chosen, which is why the French and VFINX sources are here too.
EVIDENCE = tuple(
    dict.fromkeys(
        (
            *MACRO_HISTORY_SOURCES,
            sources.FRED_FEDFUNDS,
            sources.FRED_GS10,
            sources.FRED_CPI,
            sources.FRENCH_FACTORS,
            sources.YAHOO_VFINX,
        )
    )
)


def spectral_radius(transition: object) -> float:
    """How persistent the fitted state is: the largest |eigenvalue| of `A`.

    Below 1 or the VAR(1) is non-stationary and the simulated path wanders without bound. How
    close it sits to 1 is how long a run of high inflation can persist, which is the property
    this whole state exists to carry.
    """

    return float(np.max(np.abs(np.linalg.eigvals(np.asarray(transition, dtype=float)))))


def stationary_sigma(fit: MacroVarFit) -> list[float]:
    """Each state's unconditional standard deviation under the fitted process.

    Solves the discrete Lyapunov equation `S = A S A' + L L'` by iteration rather than in closed
    form: it converges geometrically at the spectral radius, and a fitted VAR that does not
    converge here is one whose sampled paths would not settle either — worth surfacing as a
    number rather than hiding behind a solver.
    """

    transition = np.asarray(fit.transition, dtype=float)
    cholesky = np.asarray(fit.shock_cholesky, dtype=float)
    covariance = cholesky @ cholesky.T
    state = covariance.copy()
    for _ in range(10_000):
        updated = transition @ state @ transition.T + covariance
        if np.allclose(updated, state, rtol=1e-12, atol=1e-18):
            state = updated
            break
        state = updated
    return [float(value) for value in np.sqrt(np.diag(state))]


def describe(label: str, fitted: StructuralMacroFittedDefaults, fit: MacroVarFit) -> str:
    sigmas = stationary_sigma(fit)
    shocks = np.sqrt(
        np.diag(np.asarray(fit.shock_cholesky, dtype=float) @ np.asarray(fit.shock_cholesky, dtype=float).T)
    )
    lines = [
        f"{label}",
        f"  span            {fit.first_month} .. {fit.latest_month}  ({fitted.macro_state_fit.sample_months} months)",
        f"  sources         {fitted.macro_state_fit.source}",
        f"  persistence     spectral radius {spectral_radius(fit.transition):.4f}",
    ]
    lines.extend(
        f"  {name:<15} shock sd {shock:.5f}   stationary sd {sigma:.5f}"
        for name, shock, sigma in zip(STATE_NAMES, shocks.tolist(), sigmas, strict=True)
    )
    return "\n".join(lines)


async def compare_windows() -> dict[MacroFitWindow, StructuralMacroFittedDefaults]:
    """Fit both windows off one evidence snapshot, so a difference cannot be a fetch difference."""

    with tempfile.TemporaryDirectory() as directory:
        evidence_dir = Path(directory)
        await snapshot_evidence(evidence_dir, EVIDENCE)
        return {window: fit_structural_macro_defaults(evidence_dir, macro_window=window) for window in MacroFitWindow}


def fitted_var(fitted: StructuralMacroFittedDefaults) -> MacroVarFit:
    """The VAR as the simulator would ship it.

    Read back off the provider spec rather than kept from the fitter, so what is compared is what
    a run would actually sample from, not an intermediate the spec might not preserve.
    """

    return MacroVarFit(
        intercept=fitted.macro_state.intercept,
        transition=fitted.macro_state.transition,
        shock_cholesky=fitted.macro_state.shock_cholesky,
        latest_state=fitted.macro_state.initial_state,
        first_month=fitted.macro_state_fit.first_month,
        latest_month=fitted.macro_state_fit.last_month,
        sample_months=fitted.macro_state_fit.sample_months,
    )
