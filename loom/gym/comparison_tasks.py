"""Cross-series relative questions — the era factor cancels inside each question.

Each task asks whether series A gains more in percent than series B over the
same 12-month window. Macro conditions shared by both legs (the era's overall
direction) cancel out of the comparison, so memorized era priors help less
than for single-series questions. All pair questions at one anchor share a
`bundle_id`.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from loom.gym.monthly_series import MonthlySeries, add_months, month_end
from loom.gym.task import BinaryOutcome, BinaryQuestion, Task

HORIZON_MONTHS = 12

# (A, B) series ids per question: "will A out-gain B over the window?"
PAIRS = (("btcusd", "sp500"), ("eth", "sp500"), ("eth", "btcusd"), ("sp500", "cpi"))


def comparison_task(a: MonthlySeries, b: MonthlySeries, anchor: date) -> Task | None:
    """The one A-vs-B task at `anchor`, or None when any of the four needed months is unobserved."""
    target = add_months(anchor, HORIZON_MONTHS)
    a_anchor, a_target = a.values.get(anchor), a.values.get(target)
    b_anchor, b_target = b.values.get(anchor), b.values.get(target)
    if a_anchor is None or a_target is None or b_anchor is None or b_target is None:
        return None
    return Task(
        task_id=f"cmp-{a.series_id}-vs-{b.series_id}-{anchor:%Y-%m}-h{HORIZON_MONTHS}",
        as_of=add_months(anchor, 1),
        resolution_date=month_end(target),
        question=BinaryQuestion(
            text=(
                f"As of {anchor:%Y-%m} the {a.description} is {a_anchor:,.2f} and the {b.description} is "
                f"{b_anchor:,.2f}. Will the {a.description} have a greater percentage change than the "
                f"{b.description} from {anchor:%Y-%m} to {target:%Y-%m}?"
            )
        ),
        outcome=BinaryOutcome(value=a_target / a_anchor > b_target / b_anchor),
        outcome_source=f"computed from {a.provenance} and {b.provenance}",
        bundle_id=f"compare-bundle-{anchor:%Y-%m}",
    )


def comparison_tasks(
    series: Sequence[MonthlySeries], anchor_start: date = date(2016, 3, 1), anchor_step_months: int = 3
) -> tuple[Task, ...]:
    by_id = {one_series.series_id: one_series for one_series in series}
    pairs = tuple((by_id[a_id], by_id[b_id]) for a_id, b_id in PAIRS)
    last_anchor = max(min(a.last_month(), b.last_month()) for a, b in pairs)
    tasks: list[Task] = []
    anchor = anchor_start
    while anchor <= last_anchor:
        tasks.extend(task for a, b in pairs if (task := comparison_task(a, b, anchor)) is not None)
        anchor = add_months(anchor, anchor_step_months)
    return tuple(tasks)
