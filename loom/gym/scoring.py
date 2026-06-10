"""Contestant answers and proper losses for gym tasks.

Lower is better for every metric. Binary answers are scored with log loss and
Brier; scalar answers are stated as quantiles and scored with mean pinball loss
(the proper score for stated quantiles).
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from loom.gym.task import BinaryOutcome, CategoricalOutcome, CategoricalQuestion, ScalarOutcome, Task

# Probabilities are clamped away from {0, 1} so a hard-wrong binary answer
# scores a large finite log loss instead of infinity.
_LOG_LOSS_EPSILON = 1e-6

# A nonpositive stated quantile for a positive-valued quantity is clamped here
# before the log transform — maximally wrong but finite, like the log-loss clamp.
_POSITIVE_EPSILON = 1e-9

QUANTILE_LEVELS = (0.1, 0.25, 0.5, 0.75, 0.9)


class BinaryAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["binary"] = "binary"
    # Strictly in (0, 1): p=0/1 are numerically degenerate (infinite log loss)
    # and express impossible certainty; a forecaster must state a real probability.
    p: float = Field(
        gt=0.0, lt=1.0, description="Stated probability that the question resolves YES, strictly in (0, 1)."
    )


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


class CategoricalAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["categorical"] = "categorical"
    probabilities: dict[str, float] = Field(description="Category label → stated probability.")

    @model_validator(mode="after")
    def _check_probabilities(self) -> CategoricalAnswer:
        if not self.probabilities:
            raise ValueError("probabilities must be non-empty")
        if any(probability < 0.0 for probability in self.probabilities.values()):
            raise ValueError(f"probabilities must be non-negative: {self.probabilities=}")
        # Models are sloppy about exact normalization; scoring renormalizes, but a
        # wildly-off total signals a malformed answer rather than rounding.
        if abs(sum(self.probabilities.values()) - 1.0) > 0.02:
            raise ValueError(f"probabilities must sum to ~1: {sum(self.probabilities.values())=}")
        return self


Answer = Annotated[BinaryAnswer | QuantileAnswer | CategoricalAnswer, Field(discriminator="kind")]


@dataclass(frozen=True)
class TaskScore:
    task_id: str
    metrics: dict[str, float]


def pinball_loss(level: float, stated: float, realized: float) -> float:
    """Pinball (quantile) loss for one stated quantile; proper for that level."""
    return max(level * (realized - stated), (level - 1.0) * (realized - stated))


def cluster_bootstrap_ci(
    values_by_cluster: Sequence[Sequence[float]], n_resamples: int = 2000, seed: int = 0
) -> tuple[float, float] | None:
    """95% percentile-bootstrap CI of the pooled mean, resampling whole clusters.

    Tasks sharing an anchor (`as_of`) saw the same era and are correlated, so
    independence is plausible only across clusters — naive per-task bootstrap
    would be anti-conservative. Returns None with fewer than two clusters.
    """
    clusters = [list(cluster) for cluster in values_by_cluster if cluster]
    if len(clusters) < 2:
        return None
    rng = random.Random(seed)
    means = []
    for _ in range(n_resamples):
        chosen = rng.choices(clusters, k=len(clusters))
        values = [value for cluster in chosen for value in cluster]
        means.append(sum(values) / len(values))
    means.sort()
    return means[round(0.025 * (n_resamples - 1))], means[round(0.975 * (n_resamples - 1))]


def _score_categorical(task: Task, answer: CategoricalAnswer, realized_category: str) -> TaskScore:
    question = task.question
    if not isinstance(question, CategoricalQuestion):
        raise ValueError(f"categorical answer for non-categorical question: {question.kind=}")
    if set(answer.probabilities) != set(question.categories):
        raise ValueError(f"answer categories do not match question: {sorted(answer.probabilities)=}")
    total = sum(answer.probabilities.values())
    probabilities = {category: p / total for category, p in answer.probabilities.items()}
    realized_one_hot = [float(category == realized_category) for category in question.categories]
    stated = [probabilities[category] for category in question.categories]
    metrics = {
        "log_loss": -math.log(max(probabilities[realized_category], _LOG_LOSS_EPSILON)),
        "brier": sum((p - e) ** 2 for p, e in zip(stated, realized_one_hot, strict=True)),
    }
    if question.ordered:
        # Ranked probability score: squared CDF differences, normalized by K-1.
        # Proper for ordinal partitions (level/band buckets); meaningless for
        # unordered ones (joint cells), hence the gate.
        cdf_error = 0.0
        cumulative_stated = 0.0
        cumulative_realized = 0.0
        for p, e in zip(stated[:-1], realized_one_hot[:-1], strict=True):
            cumulative_stated += p
            cumulative_realized += e
            cdf_error += (cumulative_stated - cumulative_realized) ** 2
        metrics["rps"] = cdf_error / (len(stated) - 1)
    return TaskScore(task_id=task.task_id, metrics=metrics)


def score(task: Task, answer: BinaryAnswer | QuantileAnswer | CategoricalAnswer) -> TaskScore:
    match answer, task.outcome:
        case CategoricalAnswer() as categorical_answer, CategoricalOutcome(category=realized_category):
            return _score_categorical(task, categorical_answer, realized_category)
        case BinaryAnswer(p=p), BinaryOutcome(value=value):
            p_realized = p if value else 1.0 - p
            log_loss = -math.log(max(p_realized, _LOG_LOSS_EPSILON))
            return TaskScore(task_id=task.task_id, metrics={"log_loss": log_loss, "brier": (p - float(value)) ** 2})
        case QuantileAnswer(quantiles=quantiles), ScalarOutcome(value=realized):
            metrics = {
                "mean_pinball": sum(pinball_loss(level, stated, realized) for level, stated in quantiles.items())
                / len(quantiles)
            }
            # Scale-free variant: pinball in log space. Quantiles transform exactly
            # under monotone maps, so this is the proper score for stated quantiles
            # of log(value) — and it makes CPI-index and S&P-point tasks aggregate
            # comparably. Only defined for positive realized values (rates can
            # legitimately be ≤ 0).
            if realized > 0:
                metrics["mean_pinball_log"] = sum(
                    pinball_loss(level, math.log(max(stated, _POSITIVE_EPSILON)), math.log(realized))
                    for level, stated in quantiles.items()
                ) / len(quantiles)
            return TaskScore(task_id=task.task_id, metrics=metrics)
        case _:
            raise ValueError(f"answer kind does not match outcome kind: {answer.kind=} {task.outcome.kind=}")
