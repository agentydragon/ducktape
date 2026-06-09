"""Mint resolved tasks mechanically from the monthly series.

Anchors step through history; per (series, anchor) we mint a scalar
level-at-horizon question and binary threshold questions at series-specific
multipliers of the anchor level. The anchor level is embedded in the question
text — it is information dated at or before `as_of`, which any contestant is
entitled to, and without it a bare model would have to recall exact index
scales from memory.

The anchor month's close is only knowable once the month ends, so a task
anchored at month M has `as_of` = first day of M+1.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from loom.gym.bundle_tasks import bundle_tasks
from loom.gym.monthly_series import MonthlySeries, add_months, month_end
from loom.gym.seed_tasks import seed_tasks
from loom.gym.task import BinaryOutcome, BinaryQuestion, ScalarOutcome, ScalarQuestion, Task


@dataclass(frozen=True)
class SeriesTaskSpec:
    series: MonthlySeries
    # (horizon_months, multiplier-of-anchor-level) per binary threshold question.
    binary_thresholds: tuple[tuple[int, float], ...]
    scalar_horizons: tuple[int, ...] = (6,)


def default_specs(series: Sequence[MonthlySeries]) -> tuple[SeriesTaskSpec, ...]:
    by_id = {one_series.series_id: one_series for one_series in series}
    return (
        SeriesTaskSpec(series=by_id["sp500"], binary_thresholds=((6, 1.05), (12, 1.10))),
        SeriesTaskSpec(series=by_id["btcusd"], binary_thresholds=((6, 1.25), (12, 1.50))),
        SeriesTaskSpec(series=by_id["cpi"], binary_thresholds=((6, 1.02), (12, 1.035))),
    )


def tasks_for_spec(spec: SeriesTaskSpec, anchor_start: date, anchor_step_months: int) -> list[Task]:
    series = spec.series
    last_month = series.last_month()
    tasks: list[Task] = []
    anchor = anchor_start
    while anchor <= last_month:
        anchor_level = series.values.get(anchor)
        if anchor_level is None:
            anchor = add_months(anchor, anchor_step_months)
            continue
        as_of = add_months(anchor, 1)
        header = f"As of {anchor:%Y-%m} the {series.description} is {anchor_level:,.2f}."

        for horizon in spec.scalar_horizons:
            target = add_months(anchor, horizon)
            if (target_value := series.values.get(target)) is None:
                continue
            tasks.append(
                Task(
                    task_id=f"{series.series_id}-level-{anchor:%Y-%m}-h{horizon}",
                    as_of=as_of,
                    resolution_date=month_end(target),
                    question=ScalarQuestion(text=f"{header} What will it be for {target:%Y-%m}?", unit=series.unit),
                    outcome=ScalarOutcome(value=target_value),
                    outcome_source=f"computed from {series.provenance}",
                )
            )

        for horizon, multiplier in spec.binary_thresholds:
            through = add_months(anchor, horizon)
            if through > last_month:
                continue
            threshold = round(anchor_level * multiplier, 2)
            if (observed_max := series.max_observed_between(after=anchor, through=through)) is None:
                continue
            tasks.append(
                Task(
                    task_id=f"{series.series_id}-ge-{multiplier}x-{anchor:%Y-%m}-h{horizon}",
                    as_of=as_of,
                    resolution_date=month_end(through),
                    question=BinaryQuestion(
                        text=(
                            f"{header} Will it be at or above {threshold:,.2f} for any month after "
                            f"{anchor:%Y-%m}, up to and including {through:%Y-%m}?"
                        )
                    ),
                    outcome=BinaryOutcome(value=observed_max >= threshold),
                    outcome_source=f"computed from {series.provenance}",
                )
            )
        anchor = add_months(anchor, anchor_step_months)
    return tasks


# Anchors on Mar/Jun/Sep/Dec: 2024-06 lands on the grid, which is exactly
# glm-4.5's probed cutoff month (its first admissible as_of is 2024-07-01).
def series_tasks(
    series: Sequence[MonthlySeries], anchor_start: date = date(2016, 3, 1), anchor_step_months: int = 3
) -> tuple[Task, ...]:
    return tuple(
        task for spec in default_specs(series) for task in tasks_for_spec(spec, anchor_start, anchor_step_months)
    )


def all_tasks(series: Sequence[MonthlySeries]) -> tuple[Task, ...]:
    return seed_tasks() + series_tasks(series) + bundle_tasks(series)
