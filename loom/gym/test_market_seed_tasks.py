from __future__ import annotations

import pytest_bazel

from loom.gym.baseline_llm import build_prompt
from loom.gym.market_seed_tasks import MARKET_PROB_AT_AS_OF, MARKET_SEED_TASKS
from loom.gym.model_cutoffs import KNOWN_MODEL_CUTOFFS
from loom.gym.seed_tasks import SEED_TASKS
from loom.gym.task import Task

WAYBACK_PREFIX = "https://web.archive.org/web/"


def test_task_ids_unique_across_seed_families() -> None:
    ids = [task.task_id for task in SEED_TASKS + MARKET_SEED_TASKS]
    assert len(set(ids)) == len(ids)


def test_market_baseline_probs_cover_exactly_the_market_tasks() -> None:
    assert set(MARKET_PROB_AT_AS_OF) == {task.task_id for task in MARKET_SEED_TASKS}
    assert all(0 < prob < 1 for prob in MARKET_PROB_AT_AS_OF.values())


def test_market_tasks_round_trip() -> None:
    for task in MARKET_SEED_TASKS:
        assert Task.model_validate_json(task.model_dump_json()) == task


def test_market_tasks_admissible_for_every_registry_model() -> None:
    # Curation contract: tasks are recent (as_of >= 2024-09), so every model in
    # the registry — all with knowledge cutoffs <= 2024-06-30 — may forecast all
    # of them without its weights containing the outcome.
    for cutoff in KNOWN_MODEL_CUTOFFS.values():
        for task in MARKET_SEED_TASKS:
            assert cutoff.knowledge_cutoff <= task.as_of, f"{cutoff.model_id} inadmissible at {task.task_id}"


def test_market_tasks_carry_dated_wayback_evidence() -> None:
    # Curation contract: 2-5 evidence items per task, each a Wayback capture
    # whose URL timestamp agrees with the item's claimed capture date. (That
    # every date is <= as_of is enforced by the Task validator itself.)
    for task in MARKET_SEED_TASKS:
        assert 2 <= len(task.evidence) <= 5, f"{task.task_id} has {len(task.evidence)} evidence items"
        for item in task.evidence:
            assert item.url.startswith(WAYBACK_PREFIX), f"{task.task_id}: {item.url}"
            timestamp = item.url.removeprefix(WAYBACK_PREFIX).split("/", 1)[0]
            assert len(timestamp) == 14, f"{task.task_id}: {item.url}"
            assert timestamp.isdigit(), f"{task.task_id}: {item.url}"
            assert timestamp[:8] == f"{item.date:%Y%m%d}", f"{task.task_id}: URL/date mismatch for {item.url}"


def test_market_baseline_prob_never_reaches_prompts() -> None:
    # The market's own probability is a scoring reference; leaking it into the
    # prompt would hand contestants the crowd answer.
    for task in MARKET_SEED_TASKS:
        assert str(MARKET_PROB_AT_AS_OF[task.task_id]) not in build_prompt(task)


if __name__ == "__main__":
    pytest_bazel.main()
