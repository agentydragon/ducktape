from __future__ import annotations

import math
from datetime import date

import pytest
import pytest_bazel
from pydantic import ValidationError

from loom.gym.scoring import BinaryAnswer, CategoricalAnswer, QuantileAnswer, cluster_bootstrap_ci, pinball_loss, score
from loom.gym.task import (
    BinaryOutcome,
    BinaryQuestion,
    CategoricalOutcome,
    CategoricalQuestion,
    ScalarOutcome,
    ScalarQuestion,
    Task,
)


def _task(
    question: BinaryQuestion | ScalarQuestion | CategoricalQuestion,
    outcome: BinaryOutcome | ScalarOutcome | CategoricalOutcome,
) -> Task:
    return Task(
        task_id="t",
        as_of=date(2024, 7, 1),
        resolution_date=date(2024, 12, 31),
        question=question,
        outcome=outcome,
        outcome_source="test fixture",
    )


BINARY_TASK_YES = _task(BinaryQuestion(text="?"), BinaryOutcome(value=True))


def test_binary_scores() -> None:
    confident = score(BINARY_TASK_YES, BinaryAnswer(p=0.99)).metrics
    assert confident["log_loss"] == pytest.approx(-math.log(0.99))
    assert confident["brier"] == pytest.approx(0.0001)
    half = score(BINARY_TASK_YES, BinaryAnswer(p=0.5)).metrics
    assert half["log_loss"] == pytest.approx(math.log(2))
    assert half["brier"] == pytest.approx(0.25)


@pytest.mark.parametrize("p", [0.0, 1.0])
def test_binary_answer_rejects_degenerate_probability(p: float) -> None:
    # p=0/1 give infinite log loss and express impossible certainty; the answer
    # boundary requires a strict probability in (0, 1).
    with pytest.raises(ValidationError):
        BinaryAnswer(p=p)


def test_near_certain_wrong_binary_answer_scores_finite() -> None:
    # A near-0 probability on a YES outcome is clamped to a large but finite log
    # loss, not infinity (p=0 itself is rejected at the answer boundary).
    metrics = score(BINARY_TASK_YES, BinaryAnswer(p=1e-9)).metrics
    assert math.isfinite(metrics["log_loss"])
    assert metrics["log_loss"] > 10
    assert metrics["brier"] == pytest.approx(1.0)


def test_pinball_loss_is_symmetric_at_median() -> None:
    assert pinball_loss(level=0.5, stated=10.0, realized=12.0) == pytest.approx(1.0)
    assert pinball_loss(level=0.5, stated=14.0, realized=12.0) == pytest.approx(1.0)


def test_quantile_answer_scoring() -> None:
    task = _task(ScalarQuestion(text="?", unit="USD"), ScalarOutcome(value=2.0))
    answer = QuantileAnswer(quantiles={0.1: 1.0, 0.5: 2.0, 0.9: 3.0})
    assert score(task, answer).metrics["mean_pinball"] == pytest.approx(0.2 / 3)


def test_log_pinball_is_scale_free() -> None:
    # The log-space pinball of an answer is invariant under rescaling the
    # quantity's units — the property that makes CPI and S&P tasks aggregatable.
    answer = QuantileAnswer(quantiles={0.5: 110.0})
    scaled_answer = QuantileAnswer(quantiles={0.5: 110_000.0})
    small = score(_task(ScalarQuestion(text="?", unit="u"), ScalarOutcome(value=100.0)), answer)
    large = score(_task(ScalarQuestion(text="?", unit="u"), ScalarOutcome(value=100_000.0)), scaled_answer)
    assert small.metrics["mean_pinball_log"] == pytest.approx(large.metrics["mean_pinball_log"])
    assert small.metrics["mean_pinball"] != pytest.approx(large.metrics["mean_pinball"])
    # Exact at the stated quantile.
    exact = score(_task(ScalarQuestion(text="?", unit="u"), ScalarOutcome(value=110.0)), answer)
    assert exact.metrics["mean_pinball_log"] == pytest.approx(0.0)


def test_log_pinball_clamps_nonpositive_stated_values() -> None:
    task = _task(ScalarQuestion(text="?", unit="u"), ScalarOutcome(value=100.0))
    metrics = score(task, QuantileAnswer(quantiles={0.5: -5.0, 0.9: 1.0})).metrics
    assert math.isfinite(metrics["mean_pinball_log"])


def test_log_pinball_omitted_for_nonpositive_realized() -> None:
    # A realized value ≤ 0 (e.g. a negative rate) has no log; only the natural-units
    # pinball is reported then.
    task = _task(ScalarQuestion(text="?", unit="percent"), ScalarOutcome(value=-0.5))
    assert "mean_pinball_log" not in score(task, QuantileAnswer(quantiles={0.5: 1.0})).metrics


def test_quantile_answer_validation() -> None:
    with pytest.raises(ValidationError, match="non-empty"):
        QuantileAnswer(quantiles={})
    with pytest.raises(ValidationError, match="strictly in"):
        QuantileAnswer(quantiles={0.0: 1.0, 0.5: 2.0})
    # Quantile values that cross (decrease as the level increases) are incoherent.
    with pytest.raises(ValidationError, match="non-decreasing"):
        QuantileAnswer(quantiles={0.1: 5.0, 0.9: 1.0})


CATEGORICAL_TASK = _task(
    CategoricalQuestion(text="?", categories=("a", "b", "c"), ordered=True), CategoricalOutcome(category="b")
)


def test_categorical_scores() -> None:
    metrics = score(CATEGORICAL_TASK, CategoricalAnswer(probabilities={"a": 0.2, "b": 0.5, "c": 0.3})).metrics
    assert metrics["log_loss"] == pytest.approx(math.log(2))
    assert metrics["brier"] == pytest.approx(0.38)
    # RPS over cumulative buckets: ((0.2-0)^2 + (0.7-1)^2) / 2.
    assert metrics["rps"] == pytest.approx(0.065)


def test_unordered_categorical_has_no_rps() -> None:
    # RPS assumes ordinal categories; joint cells are unordered, so it is omitted.
    task = _task(CategoricalQuestion(text="?", categories=("a", "b"), ordered=False), CategoricalOutcome(category="a"))
    assert "rps" not in score(task, CategoricalAnswer(probabilities={"a": 0.5, "b": 0.5})).metrics


def test_categorical_slightly_off_total_is_renormalized() -> None:
    metrics = score(CATEGORICAL_TASK, CategoricalAnswer(probabilities={"a": 0.198, "b": 0.495, "c": 0.297})).metrics
    assert metrics["log_loss"] == pytest.approx(math.log(2))


def test_categorical_wrong_category_set_raises() -> None:
    with pytest.raises(ValueError, match="do not match"):
        score(CATEGORICAL_TASK, CategoricalAnswer(probabilities={"a": 0.5, "b": 0.5}))


def test_categorical_answer_validation() -> None:
    with pytest.raises(ValidationError, match="sum"):
        CategoricalAnswer(probabilities={"a": 0.5, "b": 0.4})
    with pytest.raises(ValidationError, match="non-negative"):
        CategoricalAnswer(probabilities={"a": 1.1, "b": -0.1})


def test_cluster_bootstrap_ci() -> None:
    # Identical clusters: every resample has the same mean, so the CI collapses.
    assert cluster_bootstrap_ci([[1.0], [1.0], [1.0]]) == (1.0, 1.0)
    ci = cluster_bootstrap_ci([[0.0], [1.0], [2.0], [3.0]], seed=7)
    assert ci is not None
    low, high = ci
    assert low <= 1.5 <= high
    assert low < high
    # A single cluster has no resampling distribution.
    assert cluster_bootstrap_ci([[1.0, 2.0]]) is None


def test_answer_outcome_kind_mismatch_raises() -> None:
    with pytest.raises(ValueError, match="does not match"):
        score(BINARY_TASK_YES, QuantileAnswer(quantiles={0.5: 1.0}))


if __name__ == "__main__":
    pytest_bazel.main()
