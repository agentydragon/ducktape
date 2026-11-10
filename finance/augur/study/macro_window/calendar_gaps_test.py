"""Calendar gaps stay gaps in VAR study forecasts and scoring horizons."""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest_bazel

from finance.augur.study.macro_window.holdout import Arm, score_arms
from finance.augur.x.models.macro_var import MacroStatePath, MacroVarFit
from finance.augur.x.models.synthetic_rates import months


def test_holdout_horizon_targets_the_calendar_month_and_omits_an_absent_target() -> None:
    all_months = months(255)
    missing_target = all_months[251]
    kept_months = tuple(month for month in all_months if month != missing_target)
    states = np.random.default_rng(8).normal(0.0, 0.01, (len(kept_months), 3))
    path = MacroStatePath(months=kept_months, states=states)

    def fixed_fit(sample: MacroStatePath) -> MacroVarFit:
        return MacroVarFit(
            intercept=(0.0, 0.0, 0.0),
            transition=((0.5, 0.0, 0.0), (0.0, 0.5, 0.0), (0.0, 0.0, 0.5)),
            shock_cholesky=((0.01, 0.0, 0.0), (0.0, 0.01, 0.0), (0.0, 0.0, 0.01)),
            latest_state=sample.state_at(-1),
            first_month=sample.months[0],
            latest_month=sample.months[-1],
            sample_months=len(sample.months),
        )

    arm = Arm(label="fixed", shortest_window_start=date(1900, 1, 1), fit=fixed_fit)
    scores = score_arms(path, [arm], horizons=(12,))

    # The first fittable state is index 239. Its 12-calendar-month target is the absent source
    # month, so it is skipped. Index arithmetic would incorrectly score the following January.
    assert [score.origins for score in scores] == [3]
    assert np.isfinite(scores[0].mean_log_density)


if __name__ == "__main__":
    pytest_bazel.main()
