"""Score one comparison over several scoring periods, and report which conclusions survive.

The window comparison turned out to depend on the months it was scored over. The two
single-window arms are identical in definition, and at ten years the 1955 window leads on
origins from 1975 and trails on origins from 1995 — so a ranking read off one period is a
statement about that period, not about the windows.

That makes the scoring period a knob, not a detail. This runs the same arms over several origin
sets and reports, per comparison and horizon, whether the sign of the difference held. A
comparison that flips is not a weak result; it is an unresolved one, and saying so is the point
of the module.

What it deliberately does NOT do is pick a period. There is no principled "right" one — a longer
history buys origins but spends them on regimes that may not recur, and every choice trades the
Volcker disinflation against the ZIRP era. Reporting the sensitivity is honest; hiding it behind
a default would not be.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from finance.augur.fit.macro_var import MACRO_STATE_NAMES, MacroStatePath
from finance.augur.study.macro_window.holdout import (
    FRED_WINDOW_START,
    HORIZONS,
    LONG_ARM,
    SHORT_ARM,
    Arm,
    ArmScore,
    score_arms,
    single_window,
)

logger = logging.getLogger(__name__)

# The earliest is where the two single-window arms first become fittable; the latest is where the
# ten-year ranking was seen to reverse. The middle one is there so a flip can be located rather
# than only detected.
ORIGIN_STARTS = (date(1975, 7, 1), date(1985, 1, 1), date(1995, 1, 1))

JOINT_DENSITY = "joint log density"


@dataclass(frozen=True)
class Comparison:
    """One arm pair, one horizon, one metric, and its sign across the scoring periods.

    `differences` is `left - right` per origin set, in the same order as the periods. The metric
    decides which sign is better, so this records the raw difference and lets the reader apply
    that; what matters here is only whether the sign HELD.
    """

    left: str
    right: str
    horizon: int
    metric: str
    differences: tuple[float, ...]

    @property
    def flips(self) -> bool | None:
        """Whether the sign changed. NOT whether the difference was material.

        A comparison whose difference is near zero in every period "holds" trivially — the sign
        is consistent because there is nothing there. The differences are reported alongside so a
        reader can see which case they are looking at; a stable sign on a hair is not evidence of
        a stable ordering.
        `None` means at least one difference is non-finite: its sign cannot establish
        stability. Such periods remain in the output, not dropped from the comparison.
        """

        if not self.differences or not all(math.isfinite(difference) for difference in self.differences):
            return None
        return len({difference > 0.0 for difference in self.differences}) > 1


def _metric_values(score: ArmScore) -> dict[str, float]:
    return {JOINT_DENSITY: score.mean_log_density, **score.mean_crps}


def compare_across_periods(
    path: MacroStatePath, arms: Sequence[Arm], *, origin_starts: Sequence[date] = ORIGIN_STARTS
) -> list[Comparison]:
    """Score `arms` once per origin set, then diff every pair on every horizon and metric."""

    per_period = []
    for start in origin_starts:
        logger.info("scoring origins from %s", start)
        scores = score_arms(path, arms, first_origin_month=start)
        per_period.append({(score.arm, score.horizon): _metric_values(score) for score in scores})

    metrics = (JOINT_DENSITY, *MACRO_STATE_NAMES)
    return [
        Comparison(
            left=left.label,
            right=right.label,
            horizon=horizon,
            metric=metric,
            differences=tuple(
                period[left.label, horizon][metric] - period[right.label, horizon][metric] for period in per_period
            ),
        )
        for index, left in enumerate(arms)
        for right in arms[index + 1 :]
        for horizon in HORIZONS
        for metric in metrics
    ]


def describe(comparisons: Sequence[Comparison], *, origin_starts: Sequence[date] = ORIGIN_STARTS) -> str:
    periods = "  ".join(f"{start:%Y}" for start in origin_starts)
    lines = [f"origin sets: {periods}", ""]
    lines.extend(
        f"  {('UNDEFINED' if comparison.flips is None else 'FLIPS' if comparison.flips else 'holds'):<9} "
        f"{comparison.left} vs {comparison.right}  "
        f"h={comparison.horizon:<4} {comparison.metric:<16} "
        + "  ".join(f"{difference:+.5f}" for difference in comparison.differences)
        for comparison in comparisons
    )
    defined = [comparison for comparison in comparisons if comparison.flips is not None]
    flipped = sum(1 for comparison in defined if comparison.flips)
    lines.append("")
    lines.append(f"{flipped} of {len(defined)} defined comparisons change sign across the scoring periods.")
    lines.append(
        f"{len(comparisons) - len(defined)} comparisons have undefined stability (non-finite or no differences)."
    )
    lines.append("'holds' means the sign was consistent, not that the gap was material — read the numbers.")
    return "\n".join(lines)


def single_window_stability(path: MacroStatePath) -> list[Comparison]:
    """The comparison the record already carries: the century against the 1955 window."""

    return compare_across_periods(
        path, [single_window(LONG_ARM, path.months[0]), single_window(SHORT_ARM, FRED_WINDOW_START)]
    )
