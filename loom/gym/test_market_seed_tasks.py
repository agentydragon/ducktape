from __future__ import annotations

import pytest_bazel

from finance.evidence.markets import Platform, load_roster
from loom.gym.baseline_llm import build_prompt
from loom.gym.market_seed_tasks import MARKET_PROB_AT_AS_OF, MARKET_SEED_RECORDS, MARKET_SEED_TASKS
from loom.gym.model_cutoffs import KNOWN_MODEL_CUTOFFS
from loom.gym.seed_tasks import SEED_TASKS
from loom.gym.task import WAYBACK_PREFIX, Task
from util.bazel.runfiles import get_required_path


def test_task_ids_unique_across_seed_families() -> None:
    ids = [task.task_id for task in SEED_TASKS + MARKET_SEED_TASKS]
    assert len(set(ids)) == len(ids)


def test_market_ids_unique() -> None:
    # The records double as the manifold-mirror roster seed; duplicate market
    # ids would mean two tasks silently sharing one upstream market.
    market_ids = [record.market_id for record in MARKET_SEED_RECORDS]
    assert len(set(market_ids)) == len(market_ids)


def test_panel_markets_are_in_the_deployed_mirror_roster() -> None:
    """Every panel market must be deep-rostered in the deployed mirror ConfigMap
    (cluster/k8s/evidence/market-roster/), so panel data stays reproducible from the
    mirror instead of trusted from one-off fetches."""
    roster = load_roster(get_required_path("_main/cluster/k8s/evidence/market-roster/market-roster.yaml"))
    rostered = {entry.market_id for entry in roster if entry.platform is Platform.MANIFOLD and entry.deep}
    missing = {record.market_id for record in MARKET_SEED_RECORDS} - rostered
    assert not missing, f"panel markets missing from the mirror roster ConfigMap: {sorted(missing)}"


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


def test_market_task_evidence_original_urls_and_bounded() -> None:
    # Curation contract: evidence is optional per task (contestants fetch
    # sources themselves through the wayback proxy), capped at 5 items, each
    # carrying the original page URL — never the archived form. Pin
    # consistency (archived_url is a capture of exactly url, on date <= as_of)
    # is enforced by the EvidenceItem and Task validators.
    assert any(task.evidence for task in MARKET_SEED_TASKS)
    for task in MARKET_SEED_TASKS:
        assert len(task.evidence) <= 5, f"{task.task_id} has {len(task.evidence)} evidence items"
        for item in task.evidence:
            assert not item.url.startswith(WAYBACK_PREFIX), f"{task.task_id}: archived form in url: {item.url}"


def test_market_baseline_prob_never_reaches_prompts() -> None:
    # The market's own probability is a scoring reference; leaking it into the
    # prompt would hand contestants the crowd answer.
    for task in MARKET_SEED_TASKS:
        assert str(MARKET_PROB_AT_AS_OF[task.task_id]) not in build_prompt(task)


if __name__ == "__main__":
    pytest_bazel.main()
