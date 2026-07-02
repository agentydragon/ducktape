from __future__ import annotations

from datetime import date

import pytest_bazel

from loom.gym.inspect_harness import headline_metric
from loom.gym.scoring import CategoricalAnswer, score
from loom.gym.task import CategoricalOutcome, CategoricalQuestion, Task


def _categorical_task(*, ordered: bool) -> Task:
    return Task(
        task_id="t",
        as_of=date(2024, 7, 1),
        resolution_date=date(2024, 12, 31),
        question=CategoricalQuestion(text="?", categories=("a", "b", "c"), ordered=ordered),
        outcome=CategoricalOutcome(category="b"),
        outcome_source="test fixture",
    )


def test_categorical_headline_metric_resolves():
    # Regression: headline_metric fell through to "mean_pinball" for categorical
    # tasks, but _score_categorical only emits log_loss/brier[/rps], so the
    # score-time lookup raised KeyError for every categorical task.
    metrics = score(
        _categorical_task(ordered=True), CategoricalAnswer(probabilities={"a": 0.2, "b": 0.5, "c": 0.3})
    ).metrics
    key = headline_metric(metrics, "categorical")
    assert key == "log_loss"
    assert key in metrics


if __name__ == "__main__":
    pytest_bazel.main()
