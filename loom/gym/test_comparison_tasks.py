from __future__ import annotations

from datetime import date

import pytest_bazel

from loom.gym.comparison_tasks import comparison_task, comparison_tasks
from loom.gym.monthly_series import MonthlySeries, add_months
from loom.gym.task import BinaryOutcome


def _series(series_id: str, values: dict[date, float]) -> MonthlySeries:
    return MonthlySeries(
        series_id=series_id,
        description=f"test {series_id}",
        unit="units",
        provenance=f"synthetic {series_id}",
        values=values,
    )


def test_outcome_compares_percentage_changes() -> None:
    # A gains 10%, B gains 5% → A out-gains B; with the legs swapped the answer flips.
    a = _series("btcusd", {date(2020, 1, 1): 100.0, date(2021, 1, 1): 110.0})
    b = _series("sp500", {date(2020, 1, 1): 200.0, date(2021, 1, 1): 210.0})
    task = comparison_task(a, b, anchor=date(2020, 1, 1))
    assert task is not None
    assert task.task_id == "cmp-btcusd-vs-sp500-2020-01-h12"
    assert task.bundle_id == "compare-bundle-2020-01"
    assert task.as_of == date(2020, 2, 1)
    assert task.resolution_date == date(2021, 1, 31)
    # Both anchor levels are stated in the question.
    assert "100.00" in task.question.text
    assert "200.00" in task.question.text
    assert task.outcome == BinaryOutcome(value=True)
    assert task.outcome_source == "computed from synthetic btcusd and synthetic sp500"

    swapped = comparison_task(b, a, anchor=date(2020, 1, 1))
    assert swapped is not None
    assert swapped.outcome == BinaryOutcome(value=False)


def test_missing_month_yields_no_task() -> None:
    a = _series("btcusd", {date(2020, 1, 1): 100.0})  # +12m target month missing
    b = _series("sp500", {date(2020, 1, 1): 200.0, date(2021, 1, 1): 210.0})
    assert comparison_task(a, b, anchor=date(2020, 1, 1)) is None


def test_comparison_tasks_grid_and_bundles() -> None:
    # 16 months of data: anchors 2020-01 and 2020-04 have +12m targets; all four pairs emit at each.
    months = {add_months(date(2020, 1, 1), n): 100.0 + n for n in range(16)}
    series = [_series(series_id, months) for series_id in ("btcusd", "sp500", "eth", "cpi")]
    tasks = comparison_tasks(series, anchor_start=date(2020, 1, 1), anchor_step_months=3)
    assert len(tasks) == 8
    assert len({task.task_id for task in tasks}) == 8
    assert {task.bundle_id for task in tasks} == {"compare-bundle-2020-01", "compare-bundle-2020-04"}


if __name__ == "__main__":
    pytest_bazel.main()
