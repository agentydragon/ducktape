from __future__ import annotations

from datetime import date

import pytest_bazel

from loom.gym.monthly_series import MonthlySeries, add_months
from loom.gym.panel import build_panel
from loom.gym.seed_tasks import SEED_TASKS
from loom.gym.series_tasks import all_tasks
from loom.gym.task import GridShape

# Four years of synthetic monthlies for every catalog series id, with distinct
# slopes so threshold/bucket outcomes vary. Anchors from the default 2016-03
# quarterly grid land inside this range with room for +24m horizons.
_SLOPES = {"sp500": 12.0, "spy": 1.5, "btcusd": 60.0, "eth": 9.0, "cpi": 0.4, "mortgage30": 0.02, "sfxrsa": 0.8}

SERIES = tuple(
    MonthlySeries(
        series_id=series_id,
        description=f"synthetic {series_id}",
        unit="units",
        provenance="synthetic",
        values={add_months(date(2016, 1, 1), n): 100.0 + slope * n for n in range(48)},
    )
    for series_id, slope in _SLOPES.items()
)

GRID = all_tasks(SERIES)
PANEL = build_panel(GRID)


def test_panel_is_deterministic_ordered_subset() -> None:
    assert build_panel(GRID) == PANEL
    grid_ids = [task.task_id for task in GRID]
    panel_ids = [task.task_id for task in PANEL]
    assert sorted(panel_ids, key=grid_ids.index) == panel_ids
    assert set(panel_ids) <= set(grid_ids)


def test_panel_compresses_the_grid() -> None:
    # The point of the panel: a real reduction in elicitation volume. The
    # bound is loose on purpose — rule tweaks shouldn't break this test
    # unless they stop compressing at all.
    assert len(PANEL) < 0.4 * len(GRID)


def test_correlated_variants_are_dropped() -> None:
    panel_shapes = {task.grid.shape for task in PANEL if task.grid is not None}
    assert GridShape.DIRECTION not in panel_shapes
    assert GridShape.BAND_PARTITION not in panel_shapes
    # h6 ceilings are near-duplicates of the kept h12 ceilings.
    assert not [
        task
        for task in PANEL
        if task.grid is not None and task.grid.shape == GridShape.CEILING and task.grid.horizon_months == 6
    ]


def test_high_information_families_are_kept() -> None:
    grid_cmp = {task.task_id for task in GRID if task.grid is not None and task.grid.shape == GridShape.COMPARISON}
    panel_cmp = {task.task_id for task in PANEL if task.grid is not None and task.grid.shape == GridShape.COMPARISON}
    assert grid_cmp
    assert panel_cmp == grid_cmp
    # Hand-curated tasks carry no grid coordinates and are always kept.
    assert {task.task_id for task in SEED_TASKS} <= {task.task_id for task in PANEL}


def test_panel_covers_every_retained_shape() -> None:
    panel_shapes = {task.grid.shape for task in PANEL if task.grid is not None}
    assert {
        GridShape.CEILING,
        GridShape.FLOOR,
        GridShape.SCALAR_LEVEL,
        GridShape.LEVEL_PARTITION,
        GridShape.JOINT,
        GridShape.DRAWDOWN,
        GridShape.FIRST_CROSS,
        GridShape.YOY,
        GridShape.COMPARISON,
    } <= panel_shapes


def test_dense_families_thin_to_sparse_anchor_grids() -> None:
    for task in PANEL:
        if task.grid is None or task.grid.shape == GridShape.COMPARISON:
            continue
        if task.grid.shape in (GridShape.CEILING, GridShape.SCALAR_LEVEL):
            assert task.grid.anchor.month in (3, 9), f"off the semi-annual grid: {task.task_id}"
        else:
            assert task.grid.anchor.month == 3, f"off the annual grid: {task.task_id}"


if __name__ == "__main__":
    pytest_bazel.main()
