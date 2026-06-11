"""Runtime conditioning observations for trained exogenous providers."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from pydantic import Field, model_validator

from finance.augur.model.schemas import FrozenModel


class ObservationTreatment(StrEnum):
    HARD_START = "hard_start"
    NOISY_MARK = "noisy_mark"
    INFORMATIVE = "informative"


class ExogenousObservedPoint(FrozenModel):
    value: float = Field(gt=0)
    observed_at: date
    source_id: str = Field(min_length=1)
    treatment: ObservationTreatment = ObservationTreatment.NOISY_MARK
    log_sigma: float | None = Field(default=None, gt=0)
    notes: str = ""


class ExogenousConditioningContext(FrozenModel):
    start_at: date
    observations: dict[str, tuple[ExogenousObservedPoint, ...]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _sort_observations(self) -> ExogenousConditioningContext:
        ordered = {
            series_id: tuple(sorted(points, key=lambda point: point.observed_at))
            for series_id, points in self.observations.items()
        }
        object.__setattr__(self, "observations", ordered)
        return self


@dataclass(frozen=True)
class NormalizedObservation:
    series_id: str
    value: float
    observed_at: date
    source_id: str
    treatment: ObservationTreatment
    log_sigma: float | None
    notes: str = ""


def iter_observations(context: ExogenousConditioningContext) -> Iterator[NormalizedObservation]:
    for series_id, points in context.observations.items():
        for point in points:
            yield NormalizedObservation(
                series_id=series_id,
                value=point.value,
                observed_at=point.observed_at,
                source_id=point.source_id,
                treatment=point.treatment,
                log_sigma=point.log_sigma,
                notes=point.notes,
            )


def latest_observations_by_series(context: ExogenousConditioningContext) -> dict[str, ExogenousObservedPoint]:
    return {
        series_id: max(
            (point for point in points if point.observed_at <= context.start_at), key=lambda point: point.observed_at
        )
        for series_id, points in context.observations.items()
        if any(point.observed_at <= context.start_at for point in points)
    }
