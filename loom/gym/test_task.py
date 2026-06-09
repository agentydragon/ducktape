from __future__ import annotations

from datetime import date

import pytest
import pytest_bazel
from pydantic import ValidationError

from loom.gym.seed_tasks import seed_tasks
from loom.gym.task import (
    BinaryOutcome,
    BinaryQuestion,
    CategoricalOutcome,
    CategoricalQuestion,
    ScalarOutcome,
    ScalarQuestion,
    Task,
)


def _binary_task(**overrides: object) -> Task:
    fields: dict[str, object] = {
        "task_id": "t1",
        "as_of": date(2024, 7, 1),
        "resolution_date": date(2024, 12, 31),
        "question": BinaryQuestion(text="Will it happen by 2024-12-31?"),
        "outcome": BinaryOutcome(value=True),
        "outcome_source": "test fixture",
    }
    fields.update(overrides)
    return Task.model_validate(fields)


def test_question_outcome_kind_mismatch_rejected() -> None:
    with pytest.raises(ValidationError, match="kind mismatch"):
        _binary_task(outcome=ScalarOutcome(value=1.0))
    with pytest.raises(ValidationError, match="kind mismatch"):
        _binary_task(question=ScalarQuestion(text="How much?", unit="USD"))


def test_categorical_outcome_must_be_a_category() -> None:
    with pytest.raises(ValidationError, match="outcome category"):
        _binary_task(
            question=CategoricalQuestion(text="Which?", categories=("a", "b"), ordered=True),
            outcome=CategoricalOutcome(category="z"),
        )


def test_resolution_before_cutoff_rejected() -> None:
    with pytest.raises(ValidationError, match="resolution must be after the cutoff"):
        _binary_task(resolution_date=date(2024, 7, 1))


def test_task_json_round_trip() -> None:
    task = _binary_task()
    assert Task.model_validate_json(task.model_dump_json()) == task


def test_seed_tasks_round_trip_with_unique_ids() -> None:
    tasks = seed_tasks()
    assert len({task.task_id for task in tasks}) == len(tasks)
    for task in tasks:
        assert Task.model_validate_json(task.model_dump_json()) == task


if __name__ == "__main__":
    pytest_bazel.main()
