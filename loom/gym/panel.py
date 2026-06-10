"""Curated non-redundant panel: the subset of the task grid routine LLM runs spend tokens on.

Near-duplicate tasks (adjacent thresholds/horizons on the same series and
anchor) add tokens, not statistical information — their errors are correlated
through the shared era and window. The panel keeps one representative per
correlated cluster and thins dense families to sparser anchor grids, per the
program plan's "run on a panel, not the grid" principle. Selection reads the
typed `Task.grid` coordinates stamped by the generators:

- hand-curated tasks (`grid is None`: seeds, market seeds) are always kept;
- cross-series comparisons are always kept: era differences out inside each
  task, the highest information per token in the grid;
- `direction` is dropped (implied by the level partition over the same
  window) and `band` is dropped in favor of `joint` (which prices the same
  volatility bucket jointly with the level);
- the 12-month ceiling and the 6-month scalar level are the semi-annual
  workhorses (March/September anchors); floors, the short/long-horizon
  probes (h3/h24), the bundle partitions, and the path shapes run annually
  (March anchors).

The full grid remains available for the classical contestant (free) and
occasional deep runs; the panel is an elicitation budget, not a redefinition
of the gym.
"""

from __future__ import annotations

from collections.abc import Sequence

from loom.gym.task import GridShape, Task

_SEMIANNUAL_ANCHOR_MONTHS = (3, 9)
_ANNUAL_ANCHOR_MONTH = 3


def _keep(task: Task) -> bool:
    if task.grid is None:
        return True
    semiannual = task.grid.anchor.month in _SEMIANNUAL_ANCHOR_MONTHS
    annual = task.grid.anchor.month == _ANNUAL_ANCHOR_MONTH
    match task.grid.shape:
        case GridShape.COMPARISON:
            return True
        case GridShape.DIRECTION | GridShape.BAND_PARTITION:
            return False
        case GridShape.CEILING:
            return (task.grid.horizon_months == 12 and semiannual) or (task.grid.horizon_months in (3, 24) and annual)
        case GridShape.SCALAR_LEVEL:
            return (task.grid.horizon_months == 6 and semiannual) or (task.grid.horizon_months == 24 and annual)
        case GridShape.FLOOR | GridShape.LEVEL_PARTITION | GridShape.JOINT:
            return annual
        case GridShape.YOY | GridShape.DRAWDOWN | GridShape.FIRST_CROSS:
            return annual


def build_panel(tasks: Sequence[Task]) -> tuple[Task, ...]:
    """Deterministic, order-preserving panel selection over generated tasks."""
    return tuple(task for task in tasks if _keep(task))
