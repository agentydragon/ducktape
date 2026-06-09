from __future__ import annotations

from datetime import date

import pytest_bazel
from more_itertools import one

from loom.gym.bundle_tasks import BundleSpec, bundle_tasks, tasks_for_bundle
from loom.gym.monthly_series import MonthlySeries, add_months
from loom.gym.task import BinaryOutcome, CategoricalOutcome, CategoricalQuestion

# Linear ramp 100, 102, ... over 2020-01..2021-06: at +12m from 2020-01 the
# level is 124 and the window band is 124 - 102 = 22.
RAMP = MonthlySeries(
    series_id="ramp",
    description="test ramp",
    unit="units",
    provenance="synthetic",
    values={add_months(date(2020, 1, 1), n): 100.0 + 2 * n for n in range(18)},
)

SPEC = BundleSpec(series=RAMP, level_multipliers=(0.95, 1.05, 1.15), band_fractions=(0.05, 0.1, 0.2))


def test_bundle_outcomes_match_series() -> None:
    tasks = {task.task_id: task for task in tasks_for_bundle(SPEC, anchor=date(2020, 1, 1))}
    assert set(tasks) == {f"ramp-bundle-2020-01-{suffix}" for suffix in ("level", "band", "dir", "joint")}
    assert all(task.bundle_id == "ramp-bundle-2020-01" for task in tasks.values())
    # Level 124 ≥ the 1.15× edge (115); band 22 ≥ the 0.2× edge (20).
    assert tasks["ramp-bundle-2020-01-level"].outcome == CategoricalOutcome(category="at or above 115.00")
    assert tasks["ramp-bundle-2020-01-band"].outcome == CategoricalOutcome(category="at or above 20.00")
    assert tasks["ramp-bundle-2020-01-dir"].outcome == BinaryOutcome(value=True)
    joint = tasks["ramp-bundle-2020-01-joint"]
    assert joint.outcome == CategoricalOutcome(category="level at or above 115.00 & band at or above 20.00")
    joint_question = joint.question
    assert isinstance(joint_question, CategoricalQuestion)
    assert not joint_question.ordered
    assert len(joint_question.categories) == 16


def test_bundle_beyond_data_not_emitted() -> None:
    # Anchor 2020-07 needs data through 2021-07; the ramp ends 2021-06.
    assert tasks_for_bundle(SPEC, anchor=date(2020, 7, 1)) == []


def test_default_bundles_generate_at_scale() -> None:
    tasks = bundle_tasks()
    assert len(tasks) > 100
    assert len({task.task_id for task in tasks}) == len(tasks)
    glm_window = [task for task in tasks if task.as_of >= date(2024, 7, 1)]
    assert len(glm_window) >= 12
    bundle = [task for task in tasks if task.bundle_id == one({t.bundle_id for t in glm_window[:1]})]
    assert {task.as_of for task in bundle} == {one({task.as_of for task in bundle})}


if __name__ == "__main__":
    pytest_bazel.main()
