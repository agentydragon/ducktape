from __future__ import annotations

from datetime import date

import pytest
import pytest_bazel
from pydantic import ValidationError

from loom.gym.baseline_llm import parse_answer, question_schema
from loom.gym.scoring import QUANTILE_LEVELS
from loom.gym.task import (
    BinaryOutcome,
    BinaryQuestion,
    CategoricalOutcome,
    CategoricalQuestion,
    ScalarOutcome,
    ScalarQuestion,
    Task,
)

BINARY_TASK = Task(
    task_id="binary-task",
    as_of=date(2024, 7, 1),
    resolution_date=date(2024, 12, 31),
    question=BinaryQuestion(text="Will the S&P 500 close above 6000 by 2024-12-31?"),
    outcome=BinaryOutcome(value=True),
    outcome_source="test fixture",
)

SCALAR_TASK = Task(
    task_id="scalar-task",
    as_of=date(2024, 7, 1),
    resolution_date=date(2024, 12, 31),
    question=ScalarQuestion(text="What will the S&P 500 close at on 2024-12-31?", unit="index points"),
    outcome=ScalarOutcome(value=5881.63),
    outcome_source="test fixture",
)

CATEGORICAL_TASK = Task(
    task_id="categorical-task",
    as_of=date(2024, 7, 1),
    resolution_date=date(2024, 12, 31),
    question=CategoricalQuestion(text="Which bucket?", categories=("low", "high"), ordered=True),
    outcome=CategoricalOutcome(category="high"),
    outcome_source="test fixture",
    bundle_id="test-bundle",
)


def test_question_schema_shapes() -> None:
    assert question_schema(BINARY_TASK.question)["required"] == ["p"]
    scalar_schema = question_schema(SCALAR_TASK.question)
    quantiles = scalar_schema["properties"]["quantiles"]  # type: ignore[index]
    assert quantiles["required"] == [str(level) for level in QUANTILE_LEVELS]
    categorical_schema = question_schema(CATEGORICAL_TASK.question)
    probabilities = categorical_schema["properties"]["probabilities"]  # type: ignore[index]
    assert probabilities["required"] == ["low", "high"]


def test_parse_answer_rejects_malformed_input() -> None:
    with pytest.raises(ValidationError):
        parse_answer(BINARY_TASK, {"p": 1.5})
    with pytest.raises(ValidationError):
        parse_answer(SCALAR_TASK, {"quantiles": {"0.9": 1.0, "0.1": 5.0}})
    with pytest.raises(ValidationError):
        parse_answer(CATEGORICAL_TASK, {"probabilities": {"low": 0.2, "high": 0.2}})


if __name__ == "__main__":
    pytest_bazel.main()
