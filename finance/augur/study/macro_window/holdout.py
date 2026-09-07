"""Does the extra 29 years predict better? Rolling-origin held-out scoring of the two windows.

`compare.py` measured how the two fits DIFFER and could not say which is right. It also named
the confound that stops its numbers from settling the question: the long record and the FRED
window do not read the same series, so any gap between them is a gap in window AND in
measurement.

This isolates the window. Both arms read the SAME series — the long record's — and differ only
in where the sample starts, so the transform, the splice and the vintage are common to both and
cancel. What is left is the one thing #5509 asks about: whether a sample reaching back through
the Depression, the 1940s inflation and the WWII peg forecasts the later record better or worse
than one starting in 1955.

Two choices worth arguing with:

- **Scored at long horizons, not just one month.** The two fits differ mainly in PERSISTENCE
  (spectral radius 0.9854 against 0.9939, a half-life of 47 months against 113). One month
  ahead that difference is nearly invisible — both predict "about what it is now" — so a
  one-step score would report a tie and hide the disagreement. `MacroVarFit.forecast` gives the
  h-step predictive in closed form, so long horizons cost no Monte-Carlo noise.
- **Expanding window, refit at every origin.** The alternative — fit once, score the tail — is
  one draw from a very autocorrelated process, and would mostly measure which regime happened
  to land in the test span.

What this cannot do: the origins overlap heavily and the horizons are long, so the scores at
successive origins are far from independent. The MEANS are comparable between arms (the arms
share every origin), but a standard error computed across origins would be badly overstated
confidence, so none is reported. A difference here is evidence, not a significance test.
"""

from __future__ import annotations

import logging
import tempfile
from bisect import bisect_left
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import jax.numpy as jnp
import numpy as np
from numpyro import distributions as dist

from finance.augur.fit.macro_var import (
    MACRO_STATE_NAMES,
    MacroStatePath,
    MacroVarFit,
    fit_macro_var_path,
    macro_state_path,
)
from finance.augur.fit.scoring import gaussian_crps, joint_log_density
from finance.augur.fit.structural_macro import macro_var_levels
from finance.augur.model.historical_windows import MACRO_HISTORY_SOURCES, load_macro_history
from finance.augur.model.structural_macro import MINIMUM_MONTHS
from finance.augur.study.trinity.evidence_snapshot import snapshot_evidence

logger = logging.getLogger(__name__)

# The first month `MacroFitWindow.FRED_1955` produces a state for. The short arm therefore
# spans the same years the shipped FRED fit does, while reading the long record's series — that
# substitution is what turns "window and measurement" into "window".
FRED_WINDOW_START = date(1955, 8, 1)

# 1 month is the conventional score and the one that will tie; 10 years is the scale a
# retirement horizon actually cares about, and where a persistence difference has had room to
# compound. The two in between are there to show which way it moves rather than only that it does.
HORIZONS = (1, 12, 60, 120)

LONG_ARM = "1926 (long record)"
SHORT_ARM = "1955 (FRED-length)"


@dataclass(frozen=True)
class ArmScore:
    """One arm's out-of-sample record at one horizon. Higher log density is better; lower CRPS
    is better, in the units of the state itself (decimal annualized rate)."""

    arm: str
    horizon: int
    origins: int
    mean_log_density: float
    mean_crps: dict[str, float]


@dataclass(frozen=True)
class Arm:
    """One fitting rule under test: a name, when it becomes fittable, and how it fits.

    A rule rather than a start date, so an arm whose equations take DIFFERENT windows
    (`mixed_windows.py`) can be scored beside the single-window ones without a second scorer.
    `shortest_window_start` is the latest start any of its equations uses — the comparison can
    only begin once every arm is fittable, and that is what decides when.
    """

    label: str
    shortest_window_start: date
    fit: Callable[[MacroStatePath], MacroVarFit]


def single_window(label: str, start: date) -> Arm:
    """An arm fitting every equation on one window."""

    return Arm(
        label=label,
        shortest_window_start=start,
        fit=lambda path: fit_macro_var_path(path.between(start, path.months[-1])),
    )


def score_arms(
    path: MacroStatePath,
    arms: Sequence[Arm],
    *,
    horizons: Sequence[int] = HORIZONS,
    first_origin_month: date | None = None,
) -> list[ArmScore]:
    """Refit every arm at every origin and score its h-step forecast against what happened.

    Every arm sees the same origins and is scored against the same observations, which is the
    only reason the means are comparable at all.

    `first_origin_month` starts the origins LATER than the arms require. That exists because the
    scoring period turned out to move the answer — a ranking measured from 1975 is not the one
    measured from 1995 — so the period has to be a knob a caller can vary rather than a
    consequence of which arms happen to be in the comparison (`stability.py`).
    """

    first_origin = _first_origin(path, latest_start=max(arm.shortest_window_start for arm in arms))
    if first_origin_month is not None:
        requested = bisect_left(path.months, first_origin_month)
        if requested < first_origin:
            raise ValueError(
                f"{first_origin_month} precedes {path.months[first_origin]}, where the arms become fittable"
            )
        first_origin = requested
    last_origin = len(path.months) - 1
    if first_origin > last_origin - min(horizons):
        raise ValueError(f"no scorable origins: {first_origin=} against {len(path.months)} states")

    densities: dict[tuple[str, int], list[float]] = {(a.label, h): [] for a in arms for h in horizons}
    crps: dict[tuple[str, int], list[dict[str, float]]] = {(a.label, h): [] for a in arms for h in horizons}

    for origin in range(first_origin, last_origin):
        for arm_rule in arms:
            arm = arm_rule.label
            fit = arm_rule.fit(path.between(path.months[0], path.months[origin]))
            for horizon in horizons:
                if origin + horizon > last_origin:
                    continue
                mean, covariance = fit.forecast(path.state_at(origin), horizon=horizon)
                predictive = dist.MultivariateNormal(loc=jnp.asarray(mean), covariance_matrix=jnp.asarray(covariance))
                observed = path.states[origin + horizon]
                densities[arm, horizon].append(joint_log_density(predictive, observed))
                crps[arm, horizon].append(gaussian_crps(predictive, observed, MACRO_STATE_NAMES))
        if (origin - first_origin) % 120 == 0:
            logger.info(
                "scored origin %s (%s of %s)", path.months[origin], origin - first_origin, last_origin - first_origin
            )

    return [
        ArmScore(
            arm=arm,
            horizon=horizon,
            origins=len(densities[arm, horizon]),
            mean_log_density=float(np.mean(densities[arm, horizon])),
            mean_crps={name: float(np.mean([row[name] for row in crps[arm, horizon]])) for name in MACRO_STATE_NAMES},
        )
        for arm in (rule.label for rule in arms)
        for horizon in horizons
    ]


def compare_start_dates(path: MacroStatePath) -> list[ArmScore]:
    """The experiment: one series, fitted from the record's own start and from 1955."""

    return score_arms(path, [single_window(LONG_ARM, path.months[0]), single_window(SHORT_ARM, FRED_WINDOW_START)])


def _first_origin(path: MacroStatePath, *, latest_start: date) -> int:
    """The earliest origin at which EVERY arm already has a fittable sample.

    Driven by the latest-starting arm: scoring an arm from before it could be fitted would
    compare a model to no model, and scoring the arms over different origins would compare them
    over different economies.
    """

    origin = bisect_left(path.months, latest_start) + MINIMUM_MONTHS - 1
    if origin >= len(path.months):
        raise ValueError(f"arm starting {latest_start} never reaches {MINIMUM_MONTHS} states in {len(path.months)}")
    return origin


def describe(scores: Sequence[ArmScore]) -> str:
    by_horizon: dict[int, list[ArmScore]] = {}
    for score in scores:
        by_horizon.setdefault(score.horizon, []).append(score)
    lines = []
    for horizon, rows in sorted(by_horizon.items()):
        lines.append(f"horizon {horizon} month(s) — {rows[0].origins} origins")
        lines.extend(
            f"  {row.arm:<20} log density {row.mean_log_density:>9.3f}   "
            + "  ".join(f"CRPS {name} {row.mean_crps[name]:.5f}" for name in MACRO_STATE_NAMES)
            for row in rows
        )
    return "\n".join(lines)


async def long_record_state_path() -> MacroStatePath:
    """The long record's macro state, fetched and assembled exactly as the fitter would."""

    with tempfile.TemporaryDirectory() as directory:
        evidence_dir = Path(directory)
        await snapshot_evidence(evidence_dir, MACRO_HISTORY_SOURCES)
        levels = macro_var_levels(load_macro_history(evidence_dir))
    return macro_state_path(
        short_rate_percent=levels.short_rate_percent,
        long_rate_percent=levels.long_rate_percent,
        cpi_level=levels.cpi_level,
    )
