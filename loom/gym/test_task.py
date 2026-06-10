from __future__ import annotations

from datetime import date

import pytest
import pytest_bazel
from pydantic import ValidationError

from loom.gym.seed_tasks import SEED_TASKS
from loom.gym.task import (
    BinaryOutcome,
    BinaryQuestion,
    CategoricalOutcome,
    CategoricalQuestion,
    EvidenceItem,
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


def _evidence(capture_date: date) -> EvidenceItem:
    return EvidenceItem(
        url="https://example.com/article",
        archived_url=f"https://web.archive.org/web/{capture_date:%Y%m%d}000000/https://example.com/article",
        date=capture_date,
        title="An article",
    )


def test_evidence_after_cutoff_rejected() -> None:
    # Capture on as_of itself is fine; one day later leaks.
    task = _binary_task(evidence=(_evidence(date(2024, 6, 1)), _evidence(date(2024, 7, 1))))
    assert len(task.evidence) == 2
    with pytest.raises(ValidationError, match="evidence dated after the cutoff"):
        _binary_task(evidence=(_evidence(date(2024, 7, 2)),))


def test_evidence_capture_pin_must_match_url_and_date() -> None:
    # The archived_url must be a Wayback capture of exactly `url`, taken on `date`.
    with pytest.raises(ValidationError, match="capture timestamp disagrees"):
        EvidenceItem(
            url="https://example.com/article",
            archived_url="https://web.archive.org/web/20240601000000/https://example.com/article",
            date=date(2024, 6, 2),
            title="An article",
        )
    with pytest.raises(ValidationError, match="archived capture is not of"):
        EvidenceItem(
            url="https://example.com/article",
            archived_url="https://web.archive.org/web/20240601000000/https://example.com/other",
            date=date(2024, 6, 1),
            title="An article",
        )
    with pytest.raises(ValidationError, match="not a Wayback capture URL"):
        EvidenceItem(
            url="https://example.com/article",
            archived_url="https://example.com/article",
            date=date(2024, 6, 1),
            title="An article",
        )


def test_task_json_round_trip() -> None:
    task = _binary_task()
    assert Task.model_validate_json(task.model_dump_json()) == task


def test_seed_tasks_round_trip_with_unique_ids() -> None:
    tasks = SEED_TASKS
    assert len({task.task_id for task in tasks}) == len(tasks)
    for task in tasks:
        assert Task.model_validate_json(task.model_dump_json()) == task


if __name__ == "__main__":
    pytest_bazel.main()
