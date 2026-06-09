"""Contestant answers and proper losses for gym tasks.

Lower is better for every metric. Binary answers are scored with log loss and
Brier; scalar answers are stated as quantiles and scored with mean pinball loss
(the proper score for stated quantiles).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from loom.gym.task import BinaryOutcome, ScalarOutcome, Task

# Probabilities are clamped away from {0, 1} so a hard-wrong binary answer
# scores a large finite log loss instead of infinity.
_LOG_LOSS_EPSILON = 1e-6

QUANTILE_LEVELS = (0.1, 0.25, 0.5, 0.75, 0.9)


class BinaryAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["binary"] = "binary"
    p: float = Field(ge=0.0, le=1.0, description="Stated probability that the question resolves YES.")


class QuantileAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["scalar"] = "scalar"
    quantiles: dict[float, float] = Field(description="Quantile level in (0, 1) → stated value at that level.")

    @model_validator(mode="after")
    def _check_quantiles(self) -> QuantileAnswer:
        if not self.quantiles:
            raise ValueError("quantiles must be non-empty")
        if not all(0.0 < level < 1.0 for level in self.quantiles):
            raise ValueError(f"quantile levels must lie strictly in (0, 1): {sorted(self.quantiles)=}")
        values_by_level = [value for _, value in sorted(self.quantiles.items())]
        if values_by_level != sorted(values_by_level):
            raise ValueError(f"quantile values must be non-decreasing in level: {self.quantiles=}")
        return self


Answer = Annotated[BinaryAnswer | QuantileAnswer, Field(discriminator="kind")]


@dataclass(frozen=True)
class TaskScore:
    task_id: str
    metrics: dict[str, float]


def pinball_loss(level: float, stated: float, realized: float) -> float:
    """Pinball (quantile) loss for one stated quantile; proper for that level."""
    return max(level * (realized - stated), (level - 1.0) * (realized - stated))


def score(task: Task, answer: BinaryAnswer | QuantileAnswer) -> TaskScore:
    match answer, task.outcome:
        case BinaryAnswer(p=p), BinaryOutcome(value=value):
            p_realized = p if value else 1.0 - p
            log_loss = -math.log(max(p_realized, _LOG_LOSS_EPSILON))
            return TaskScore(task_id=task.task_id, metrics={"log_loss": log_loss, "brier": (p - float(value)) ** 2})
        case QuantileAnswer(quantiles=quantiles), ScalarOutcome(value=realized):
            mean_pinball = sum(pinball_loss(level, stated, realized) for level, stated in quantiles.items()) / len(
                quantiles
            )
            return TaskScore(task_id=task.task_id, metrics={"mean_pinball": mean_pinball})
        case _:
            raise ValueError(f"answer kind does not match outcome kind: {answer.kind=} {task.outcome.kind=}")
