"""Path-statistic questions over one series.

Where the bundle families ask about window endpoints, these ask about the
shape of the 12-month path: the realized year-over-year change bucket (CPI),
the maximum peak-to-trough drawdown bucket (equity/crypto), and the first
window month to cross the series' 12-month ceiling threshold. All tasks per
(series, anchor) join the existing `{series_id}-bundle-{anchor}` bundle.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from itertools import pairwise

from more_itertools import first

from loom.gym.bundle_tasks import bucket_for
from loom.gym.monthly_series import MonthlySeries, add_months, month_end
from loom.gym.task import CategoricalOutcome, CategoricalQuestion, Task

HORIZON_MONTHS = 12

# Realized CPI year-over-year change bucket edges, in percent.
_YOY_EDGES = (2.0, 3.0, 4.0)
# Max peak-to-trough drawdown bucket edges, as fractions of the running peak.
_DRAWDOWN_EDGES = {
    "sp500": (0.05, 0.10, 0.20),
    "spy": (0.05, 0.10, 0.20),
    "btcusd": (0.15, 0.30, 0.50),
    "eth": (0.15, 0.30, 0.50),
}
# First-cross threshold as a multiple of the anchor level: the series' 12-month
# ceiling multiplier from the binary threshold family.
_FIRST_CROSS_MULTIPLIERS = {"sp500": 1.10, "spy": 1.10, "btcusd": 1.50, "eth": 1.50}
_FIRST_CROSS_CATEGORIES = ("months 1-3", "months 4-6", "months 7-9", "months 10-12", "never")


def percent_bucket_labels(edges: tuple[float, ...]) -> tuple[str, ...]:
    """`bundle_tasks.bucket_labels` for percent-valued edges — "under 5.0%" style."""
    labels = [f"under {edges[0]:.1f}%"]
    labels += [f"{low:.1f}% to under {high:.1f}%" for low, high in pairwise(edges)]
    labels.append(f"at or above {edges[-1]:.1f}%")
    return tuple(labels)


def tasks_for_path(series: MonthlySeries, anchor: date) -> list[Task]:
    anchor_level = series.values.get(anchor)
    if anchor_level is None:
        return []
    target = add_months(anchor, HORIZON_MONTHS)
    # Path tasks join the per-(series, anchor) bundle from bundle_tasks: same
    # dossier, same as_of, so one bundled request elicits all of them together.
    bundle_id = f"{series.series_id}-bundle-{anchor:%Y-%m}"
    header = f"As of {anchor:%Y-%m} the {series.description} is {anchor_level:,.2f}."
    source = f"computed from {series.provenance}"
    as_of = add_months(anchor, 1)
    resolution = month_end(target)

    tasks: list[Task] = []
    # YoY needs only the two months 12 apart; at this horizon the baseline month is the anchor itself.
    if series.series_id == "cpi" and (target_value := series.values.get(target)) is not None:
        yoy = (target_value / anchor_level - 1.0) * 100.0
        labels = percent_bucket_labels(_YOY_EDGES)
        tasks.append(
            Task(
                task_id=f"{bundle_id}-yoy",
                as_of=as_of,
                resolution_date=resolution,
                question=CategoricalQuestion(
                    text=(
                        f"{header} Into which range will the year-over-year percentage change of the index "
                        f"for {target:%Y-%m} vs {anchor:%Y-%m} fall?"
                    ),
                    categories=labels,
                    ordered=True,
                ),
                outcome=CategoricalOutcome(category=bucket_for(yoy, _YOY_EDGES, labels)),
                outcome_source=source,
                bundle_id=bundle_id,
            )
        )

    # Drawdown and first-cross are order-sensitive path statistics: any hole in
    # the window makes them uncomputable, so those tasks are skipped entirely.
    window: list[float] = []
    for offset in range(1, HORIZON_MONTHS + 1):
        if (value := series.values.get(add_months(anchor, offset))) is None:
            return tasks
        window.append(value)

    if (drawdown_fractions := _DRAWDOWN_EDGES.get(series.series_id)) is not None:
        peak = anchor_level
        fractions = []
        for value in window:
            peak = max(peak, value)
            fractions.append((peak - value) / peak)
        edges = tuple(fraction * 100.0 for fraction in drawdown_fractions)
        labels = percent_bucket_labels(edges)
        tasks.append(
            Task(
                task_id=f"{bundle_id}-drawdown",
                as_of=as_of,
                resolution_date=resolution,
                question=CategoricalQuestion(
                    text=(
                        f"{header} Consider the largest peak-to-trough decline over the months after "
                        f"{anchor:%Y-%m} up to and including {target:%Y-%m}, with the running peak starting "
                        f"at the {anchor:%Y-%m} value. Into which range, as a percentage of the running peak, "
                        "will it fall?"
                    ),
                    categories=labels,
                    ordered=True,
                ),
                outcome=CategoricalOutcome(category=bucket_for(max(fractions) * 100.0, edges, labels)),
                outcome_source=source,
                bundle_id=bundle_id,
            )
        )

    if (multiplier := _FIRST_CROSS_MULTIPLIERS.get(series.series_id)) is not None:
        threshold = round(anchor_level * multiplier, 2)
        crossed = first((offset for offset, value in enumerate(window, start=1) if value >= threshold), default=None)
        realized = _FIRST_CROSS_CATEGORIES[-1] if crossed is None else _FIRST_CROSS_CATEGORIES[(crossed - 1) // 3]
        tasks.append(
            Task(
                task_id=f"{bundle_id}-first-cross",
                as_of=as_of,
                resolution_date=resolution,
                question=CategoricalQuestion(
                    text=(
                        f"{header} In which window of months after {anchor:%Y-%m} (if any) will its value first "
                        f"be at or above {threshold:,.2f}? Month 1 is {add_months(anchor, 1):%Y-%m}; the last "
                        f"counted month, month {HORIZON_MONTHS}, is {target:%Y-%m}."
                    ),
                    categories=_FIRST_CROSS_CATEGORIES,
                    ordered=True,
                ),
                outcome=CategoricalOutcome(category=realized),
                outcome_source=source,
                bundle_id=bundle_id,
            )
        )
    return tasks


def path_tasks(
    series: Sequence[MonthlySeries], anchor_start: date = date(2016, 3, 1), anchor_step_months: int = 3
) -> tuple[Task, ...]:
    tasks: list[Task] = []
    for one_series in series:
        anchor = anchor_start
        while anchor <= one_series.last_month():
            tasks.extend(tasks_for_path(one_series, anchor))
            anchor = add_months(anchor, anchor_step_months)
    return tuple(tasks)
