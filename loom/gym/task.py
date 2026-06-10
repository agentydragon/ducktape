"""Forecasting-gym task schema: a question asked as-of a date, plus its realized outcome.

`as_of` is the information cutoff: no data dated after it may inform a forecast,
and an LLM contestant's weights must be frozen on or before it (enforced by
`loom.gym.model_cutoffs.assert_admissible`).
"""

from __future__ import annotations

import datetime
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


class EvidenceItem(BaseModel):
    """A timestamped piece of public material a contestant may be shown.

    The Wayback capture date is an airtight "existed by then" bound, so an item
    dated at or before a task's `as_of` cannot leak post-cutoff information
    (beyond what its title states — titles must not contain post-capture facts).
    """

    model_config = ConfigDict(extra="forbid")

    url: str = Field(description="Wayback-archived form: https://web.archive.org/web/<YYYYMMDDhhmmss>/<original>.")
    # Annotated via the module to dodge the pydantic field-name/type-annotation clash (`date: date`).
    date: datetime.date = Field(description="The Wayback capture date.")
    title: str = Field(description="Short human-readable description of what the page says.")


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
    evidence: tuple[EvidenceItem, ...] = Field(
        default=(),
        description="Dated evidence captured at or before as_of (enforced), so prompts may include it unconditionally.",
    )

    @model_validator(mode="after")
    def _check_consistency(self) -> Task:
        if self.question.kind != self.outcome.kind:
            raise ValueError(f"question/outcome kind mismatch: {self.question.kind=} {self.outcome.kind=}")
        if self.resolution_date <= self.as_of:
            raise ValueError(f"resolution must be after the cutoff: {self.as_of=} {self.resolution_date=}")
        for item in self.evidence:
            if item.date > self.as_of:
                raise ValueError(f"evidence dated after the cutoff: {item.date=} {self.as_of=} {item.url=}")
        if (
            isinstance(self.question, CategoricalQuestion)
            and isinstance(self.outcome, CategoricalOutcome)
            and self.outcome.category not in self.question.categories
        ):
            raise ValueError(f"outcome category not in question categories: {self.outcome.category=}")
        return self
