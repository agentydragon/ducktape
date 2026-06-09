"""Forecasting-gym task schema: a question asked as-of a date, plus its realized outcome.

`as_of` is the information cutoff: no data dated after it may inform a forecast,
and an LLM contestant's weights must be frozen on or before it (enforced by
`loom.gym.model_cutoffs.assert_admissible`).
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BinaryQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["binary"] = "binary"
    text: str = Field(description="Full question including the resolution criterion.")


class ScalarQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["scalar"] = "scalar"
    text: str = Field(description="Full question including the resolution criterion.")
    unit: str = Field(description='Unit of the answer, e.g. "USD" or "percent".')


class CategoricalQuestion(BaseModel):
    """A partition question: exactly one category resolves true."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["categorical"] = "categorical"
    text: str = Field(description="Full question including the resolution criterion.")
    categories: tuple[str, ...] = Field(min_length=2, description="Mutually exclusive, exhaustive labels.")
    ordered: bool = Field(
        description="Whether the categories are ordinal (level/band buckets) — gates ranked probability scoring."
    )


Question = Annotated[BinaryQuestion | ScalarQuestion | CategoricalQuestion, Field(discriminator="kind")]


class BinaryOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["binary"] = "binary"
    value: bool


class ScalarOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["scalar"] = "scalar"
    value: float


class CategoricalOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["categorical"] = "categorical"
    category: str


Outcome = Annotated[BinaryOutcome | ScalarOutcome | CategoricalOutcome, Field(discriminator="kind")]


class Task(BaseModel):
    """One resolved forecasting task."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    as_of: date = Field(description="Information cutoff; only data dated on or before this may be used.")
    resolution_date: date
    question: Question
    outcome: Outcome
    outcome_source: str = Field(description="Where the realized outcome comes from, for re-verification.")
    bundle_id: str | None = Field(
        default=None,
        description="Tasks sharing a bundle_id (and as_of) may be elicited in one request; scoring stays per-task.",
    )

    @model_validator(mode="after")
    def _check_consistency(self) -> Task:
        if self.question.kind != self.outcome.kind:
            raise ValueError(f"question/outcome kind mismatch: {self.question.kind=} {self.outcome.kind=}")
        if self.resolution_date <= self.as_of:
            raise ValueError(f"resolution must be after the cutoff: {self.as_of=} {self.resolution_date=}")
        if (
            isinstance(self.question, CategoricalQuestion)
            and isinstance(self.outcome, CategoricalOutcome)
            and self.outcome.category not in self.question.categories
        ):
            raise ValueError(f"outcome category not in question categories: {self.outcome.category=}")
        return self
