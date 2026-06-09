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


Question = Annotated[BinaryQuestion | ScalarQuestion, Field(discriminator="kind")]


class BinaryOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["binary"] = "binary"
    value: bool


class ScalarOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["scalar"] = "scalar"
    value: float


Outcome = Annotated[BinaryOutcome | ScalarOutcome, Field(discriminator="kind")]


class Task(BaseModel):
    """One resolved forecasting task."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    as_of: date = Field(description="Information cutoff; only data dated on or before this may be used.")
    resolution_date: date
    question: Question
    outcome: Outcome
    outcome_source: str = Field(description="Where the realized outcome comes from, for re-verification.")

    @model_validator(mode="after")
    def _check_consistency(self) -> Task:
        if self.question.kind != self.outcome.kind:
            raise ValueError(f"question/outcome kind mismatch: {self.question.kind=} {self.outcome.kind=}")
        if self.resolution_date <= self.as_of:
            raise ValueError(f"resolution must be after the cutoff: {self.as_of=} {self.resolution_date=}")
        return self
