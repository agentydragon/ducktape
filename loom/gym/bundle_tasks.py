"""Bundle task families: many named questions over one dossier/anchor.

A bundle is a set of ordinary tasks sharing `bundle_id` (and `as_of`): they can
be elicited in a single request — more metrics per sampled token, and joint
structure becomes scoreable — while scoring stays per-task, so a question
scores identically solo or bundled.

Families per (series, anchor), horizon 12 months:

- `level` — which bucket the value for month anchor+12 lands in (ordinal);
- `band` — which bucket (highest - lowest month-end value over the window)
  lands in, with bucket edges stated as absolute values derived from the
  anchor level (ordinal);
- `dir` — value at anchor+12 at or above the anchor level (binary);
- `joint` — (level bucket × band bucket) cell (unordered) — tests whether the
  stated joint is coherent, not just the marginals.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from itertools import pairwise

from loom.gym.monthly_series import MonthlySeries, add_months, month_end
from loom.gym.task import (
    BinaryOutcome,
    BinaryQuestion,
    CategoricalOutcome,
    CategoricalQuestion,
    GridCoordinates,
    GridShape,
    Task,
)

HORIZON_MONTHS = 12


@dataclass(frozen=True)
class BundleSpec:
    series: MonthlySeries
    # Bucket edges for the level at +12m, as multiples of the anchor level.
    level_multipliers: tuple[float, ...]
    # Bucket edges for (max - min) over the window, as fractions of the anchor level.
    band_fractions: tuple[float, ...]


# mortgage30/sfxrsa stay out of bundles: their floor/ceiling questions in the
# series family already cover the interesting moves at their low volatility.
def default_bundle_specs(series: Sequence[MonthlySeries]) -> tuple[BundleSpec, ...]:
    by_id = {one_series.series_id: one_series for one_series in series}
    return (
        BundleSpec(series=by_id["sp500"], level_multipliers=(0.95, 1.05, 1.15), band_fractions=(0.05, 0.1, 0.2)),
        BundleSpec(series=by_id["spy"], level_multipliers=(0.95, 1.05, 1.15), band_fractions=(0.05, 0.1, 0.2)),
        BundleSpec(series=by_id["btcusd"], level_multipliers=(0.8, 1.2, 1.8), band_fractions=(0.2, 0.4, 0.8)),
        BundleSpec(series=by_id["eth"], level_multipliers=(0.8, 1.2, 1.8), band_fractions=(0.2, 0.4, 0.8)),
        BundleSpec(series=by_id["cpi"], level_multipliers=(1.0, 1.02, 1.04), band_fractions=(0.01, 0.02, 0.04)),
    )


def bucket_labels(edges: tuple[float, ...]) -> tuple[str, ...]:
    labels = [f"under {edges[0]:,.2f}"]
    labels += [f"{low:,.2f} to under {high:,.2f}" for low, high in pairwise(edges)]
    labels.append(f"at or above {edges[-1]:,.2f}")
    return tuple(labels)


def bucket_for(value: float, edges: tuple[float, ...], labels: tuple[str, ...]) -> str:
    for edge, label in zip(edges, labels, strict=False):
        if value < edge:
            return label
    return labels[-1]


def tasks_for_bundle(spec: BundleSpec, anchor: date) -> list[Task]:
    series = spec.series
    anchor_level = series.values.get(anchor)
    if anchor_level is None:
        return []
    target = add_months(anchor, HORIZON_MONTHS)
    target_value = series.values.get(target)
    band = None
    if (high := series.max_observed_between(after=anchor, through=target)) is not None:
        low = min(value for month, value in series.values.items() if anchor < month <= target)
        band = high - low
    if target > series.last_month():
        return []

    bundle_id = f"{series.series_id}-bundle-{anchor:%Y-%m}"
    header = f"As of {anchor:%Y-%m} the {series.description} is {anchor_level:,.2f}."
    source = f"computed from {series.provenance}"
    resolution = month_end(target)
    level_edges = tuple(round(anchor_level * multiplier, 2) for multiplier in spec.level_multipliers)
    level_labels = bucket_labels(level_edges)
    band_edges = tuple(round(anchor_level * fraction, 2) for fraction in spec.band_fractions)
    band_labels = bucket_labels(band_edges)

    tasks: list[Task] = []
    if target_value is not None:
        tasks.append(
            Task(
                task_id=f"{bundle_id}-level",
                as_of=add_months(anchor, 1),
                resolution_date=resolution,
                question=CategoricalQuestion(
                    text=f"{header} Into which range ({series.unit}) will its value for {target:%Y-%m} fall?",
                    categories=level_labels,
                    ordered=True,
                ),
                outcome=CategoricalOutcome(category=bucket_for(target_value, level_edges, level_labels)),
                outcome_source=source,
                bundle_id=bundle_id,
                grid=GridCoordinates(shape=GridShape.LEVEL_PARTITION, anchor=anchor, horizon_months=HORIZON_MONTHS),
            )
        )
        tasks.append(
            Task(
                task_id=f"{bundle_id}-dir",
                as_of=add_months(anchor, 1),
                resolution_date=resolution,
                question=BinaryQuestion(
                    text=f"{header} Will its value for {target:%Y-%m} be at or above {anchor_level:,.2f}?"
                ),
                outcome=BinaryOutcome(value=target_value >= anchor_level),
                outcome_source=source,
                bundle_id=bundle_id,
                grid=GridCoordinates(shape=GridShape.DIRECTION, anchor=anchor, horizon_months=HORIZON_MONTHS),
            )
        )
    if band is not None:
        tasks.append(
            Task(
                task_id=f"{bundle_id}-band",
                as_of=add_months(anchor, 1),
                resolution_date=resolution,
                question=CategoricalQuestion(
                    text=(
                        f"{header} Consider band = (highest month-end value) - (lowest month-end value) over the "
                        f"months after {anchor:%Y-%m} up to and including {target:%Y-%m}. "
                        f"Into which range ({series.unit}) will the band fall?"
                    ),
                    categories=band_labels,
                    ordered=True,
                ),
                outcome=CategoricalOutcome(category=bucket_for(band, band_edges, band_labels)),
                outcome_source=source,
                bundle_id=bundle_id,
                grid=GridCoordinates(shape=GridShape.BAND_PARTITION, anchor=anchor, horizon_months=HORIZON_MONTHS),
            )
        )
    if target_value is not None and band is not None:
        joint_labels = tuple(
            f"level {level_label} & band {band_label}" for level_label in level_labels for band_label in band_labels
        )
        realized_joint = (
            f"level {bucket_for(target_value, level_edges, level_labels)} "
            f"& band {bucket_for(band, band_edges, band_labels)}"
        )
        tasks.append(
            Task(
                task_id=f"{bundle_id}-joint",
                as_of=add_months(anchor, 1),
                resolution_date=resolution,
                question=CategoricalQuestion(
                    text=(
                        f"{header} Jointly: into which (level range for {target:%Y-%m}, band range) cell will it "
                        "fall? Level and band are as defined in the corresponding single questions."
                    ),
                    categories=joint_labels,
                    ordered=False,
                ),
                outcome=CategoricalOutcome(category=realized_joint),
                outcome_source=source,
                bundle_id=bundle_id,
                grid=GridCoordinates(shape=GridShape.JOINT, anchor=anchor, horizon_months=HORIZON_MONTHS),
            )
        )
    return tasks


def tasks_for_bundle_spec(spec: BundleSpec, anchor_start: date, anchor_step_months: int) -> list[Task]:
    tasks: list[Task] = []
    anchor = anchor_start
    while anchor <= spec.series.last_month():
        tasks.extend(tasks_for_bundle(spec, anchor))
        anchor = add_months(anchor, anchor_step_months)
    return tasks


def bundle_tasks(
    series: Sequence[MonthlySeries], anchor_start: date = date(2016, 3, 1), anchor_step_months: int = 3
) -> tuple[Task, ...]:
    return tuple(
        task
        for spec in default_bundle_specs(series)
        for task in tasks_for_bundle_spec(spec, anchor_start, anchor_step_months)
    )
