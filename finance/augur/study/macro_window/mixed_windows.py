"""Fit each VAR equation on its own window, and score the result against both single-window fits.

`holdout.py` found that neither window dominates: past a month, the century forecasts inflation
better and both rate states worse, at every horizon. #5817 asks the obvious follow-up — take
each equation's better window and keep it.

That is mechanically available because the estimator is one OLS per equation over shared
regressors, which for a VAR is exactly the joint estimate. Each equation can take a different
sample without changing what is being estimated.

**What is genuinely new here, and what is not.** The per-state CRPS in `holdout.py` already says
what each equation's own window buys it, so the marginals are close to settled by construction.
The open question is the part that is NOT per-equation: the innovation covariance couples the
equations, a correlation needs both residuals on the same months, and there is no reason a
covariance estimated on the overlap has to sit well with rows estimated on different spans.

What the sweep in `covariance_span_test.py` actually established is a level up from that. The
covariance span moves the first scorable origin, the origin set moves the ranking, and at ten
years it moves it far enough to reverse which single window wins — so a result from here is a
statement about a scoring period, not about a window. `model/SPEC.md` carries the numbers.

Deliberately an experiment rather than a fitter: nothing here changes the shipped
`fit_macro_var` surface, and the provenance question a mixed fit raises — `MacroVarFit`'s
`first_month` and `sample_months` describe ONE window, and a mixed fit has none — is left for
after the measurement says whether it is worth answering.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

import numpy as np

from finance.augur.fit.macro_var import MACRO_STATE_DIM, MacroStatePath, MacroVarFit, as_state_matrix, as_state_vector
from finance.augur.model.structural_macro import MINIMUM_MONTHS
from finance.augur.study.macro_window.holdout import (
    FRED_WINDOW_START,
    LONG_ARM,
    SHORT_ARM,
    Arm,
    ArmScore,
    score_arms,
    single_window,
)

logger = logging.getLogger(__name__)

MIXED_ARM = "mixed (rates 1955, inflation 1926)"


@dataclass(frozen=True)
class EquationWindows:
    """Where each equation's own sample starts, in state order."""

    short_rate: date
    term_spread: date
    inflation_rate: date

    @property
    def starts(self) -> tuple[date, date, date]:
        return (self.short_rate, self.term_spread, self.inflation_rate)


def fit_mixed_windows(
    path: MacroStatePath, windows: EquationWindows, *, covariance_start: date | None = None
) -> MacroVarFit:
    """Estimate each row of `(c, A)` on its own window; estimate `L` on the span they share.

    The covariance is the one quantity that cannot be split. Its residuals are recomputed on the
    common span using the per-equation coefficients, rather than glued together from each
    equation's own residuals: those cover different months, and a covariance assembled from
    misaligned residuals would not be one.

    `covariance_start` defaults to the overlap, where every row sits inside its own fitting
    window. Starting it EARLIER is a real option rather than an error: the residuals there come
    from applying a row's coefficients to months it was not fitted on, which is a harsher and
    arguably more honest measure of its innovation scale. So it is a parameter, and swept.

    `first_month` and `sample_months` on the result describe that covariance span — the only window
    the fit as a whole is jointly identified on. The per-equation windows are the argument, and
    the result does not carry them; a shipped version would need to.
    """

    end = path.months[-1]
    coefficients = np.empty((MACRO_STATE_DIM + 1, MACRO_STATE_DIM))
    for index, start in enumerate(windows.starts):
        window = path.between(start, end)
        if len(window.months) < MINIMUM_MONTHS:
            raise ValueError(f"equation {index} window from {start} has {len(window.months)} states")
        previous, current = window.states[:-1], window.states[1:]
        design = np.column_stack([np.ones(len(previous)), previous])
        coefficients[:, index], *_ = np.linalg.lstsq(design, current[:, index], rcond=None)

    common = path.between(covariance_start or max(windows.starts), end)
    # The covariance needs its own guard: it is the one quantity whose span is not an equation
    # window, so the loop above never checks it. Without this a late `covariance_start` estimates
    # a 3x3 covariance off a handful of residuals at the early origins — numerically fine, and
    # meaningless.
    if len(common.months) < MINIMUM_MONTHS:
        raise ValueError(f"covariance span from {common.months[0]} has {len(common.months)} states")
    previous, current = common.states[:-1], common.states[1:]
    design = np.column_stack([np.ones(len(previous)), previous])
    residuals = current - design @ coefficients
    covariance = residuals.T @ residuals / (len(residuals) - design.shape[1])

    return MacroVarFit(
        intercept=as_state_vector(coefficients[0].tolist()),
        transition=as_state_matrix(coefficients[1:].T.tolist()),
        shock_cholesky=as_state_matrix(np.linalg.cholesky(covariance).tolist()),
        latest_state=common.state_at(-1),
        first_month=common.months[0],
        latest_month=common.months[-1],
        sample_months=len(common.months),
    )


def mixed_arm(
    *, rate_start: date, inflation_start: date, covariance_start: date | None = None, label: str = MIXED_ARM
) -> Arm:
    """Each rate equation on `rate_start`, inflation on `inflation_start`.

    Which way round is not a free choice — it is what `holdout.py` measured: the rate equations
    were the ones the century hurt, inflation the one it helped.
    """

    windows = EquationWindows(short_rate=rate_start, term_spread=rate_start, inflation_rate=inflation_start)
    return Arm(
        # The covariance span counts: an arm is fittable only once EVERY span it needs is long
        # enough, and a late `covariance_start` is the binding one.
        label=label,
        shortest_window_start=max(*windows.starts, covariance_start or windows.starts[0]),
        fit=lambda path: fit_mixed_windows(path, windows, covariance_start=covariance_start),
    )


def compare_with_mixed(path: MacroStatePath) -> list[ArmScore]:
    """Both single-window arms and the mixed one, on the same origins."""

    return score_arms(
        path,
        [
            single_window(LONG_ARM, path.months[0]),
            single_window(SHORT_ARM, FRED_WINDOW_START),
            mixed_arm(rate_start=FRED_WINDOW_START, inflation_start=path.months[0]),
        ],
    )


def compare_covariance_spans(path: MacroStatePath) -> list[ArmScore]:
    """Both single windows, and the mixed fit with its covariance on several spans.

    The mixed fit beat the long record on every marginal at five years and lost to it on JOINT
    density, which is a statement about correlation rather than about any one equation. The
    covariance is the only part of the fit that is not per-equation, and its span was never
    chosen — it defaulted to the overlap. This sweeps it.
    """

    long_start = path.months[0]
    return score_arms(
        path,
        [
            single_window(LONG_ARM, long_start),
            single_window(SHORT_ARM, FRED_WINDOW_START),
            *(
                mixed_arm(
                    rate_start=FRED_WINDOW_START,
                    inflation_start=long_start,
                    covariance_start=start,
                    label=f"mixed, cov {start:%Y}",
                )
                for start in (long_start, date(1940, 1, 1), FRED_WINDOW_START, date(1975, 1, 1))
            ),
        ],
    )
