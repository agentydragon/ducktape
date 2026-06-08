"""Transfer domain decoder. The compile-side twin is `TransferCompileOutput` +
`_compile_transfers` in `augur.sim.compiler`."""

from __future__ import annotations

import numpy as np
import polars as pl

from finance.augur.sim.buffers import SimulationBuffers
from finance.augur.sim.codec.helpers import code_column, frame_from_columns, usd_column
from finance.augur.sim.compiler import CompiledSimulation
from finance.augur.sim.events import EVENT_FRAMES


def _income_category_column(mask: np.ndarray) -> pl.Series:
    values: list[str | None] = [None] * int(mask.size)
    for index in np.flatnonzero(mask):
        values[int(index)] = "ordinary"
    return pl.Series("income_category", values, dtype=pl.Utf8)


def decode_transfers(plan: CompiledSimulation, buffers: SimulationBuffers) -> pl.DataFrame:
    active = buffers.transfers.active  # (M, S, R)
    months, slots, rollouts = np.argwhere(active).T if active.any() else (np.array([], dtype=np.int64),) * 3
    amounts = usd_column(buffers.transfers.amount[months, slots, rollouts])
    income_categories = _income_category_column(plan.transfers.income_profile[months, slots] >= 0)
    return frame_from_columns(
        EVENT_FRAMES.transfers,
        rollout_index=rollouts,
        month_index=months,
        cause_id=code_column(plan, plan.transfers.cause[months, slots]),
        from_agent_id=code_column(plan, plan.transfers.from_agent[months, slots]),
        from_account_id=code_column(plan, plan.transfers.from_account[months, slots]),
        to_agent_id=code_column(plan, plan.transfers.to_agent[months, slots]),
        to_account_id=code_column(plan, plan.transfers.to_account[months, slots]),
        amount_usd=amounts,
        income_category=income_categories,
    )


def decode_property_cashflows(plan: CompiledSimulation, buffers: SimulationBuffers) -> pl.DataFrame:
    active = buffers.property_cashflows.active  # (M, S, R)
    months, slots, rollouts = np.argwhere(active).T if active.any() else (np.array([], dtype=np.int64),) * 3
    amounts = usd_column(buffers.property_cashflows.amount[months, slots, rollouts])
    cashflows = plan.property_cashflows
    income_categories = _income_category_column(cashflows.income_profile[months, slots] >= 0)
    return frame_from_columns(
        EVENT_FRAMES.transfers,
        rollout_index=rollouts,
        month_index=months,
        cause_id=code_column(plan, cashflows.cause[months, slots]),
        from_agent_id=code_column(plan, cashflows.from_agent[months, slots]),
        from_account_id=code_column(plan, cashflows.from_account[months, slots]),
        to_agent_id=code_column(plan, cashflows.to_agent[months, slots]),
        to_account_id=code_column(plan, cashflows.to_account[months, slots]),
        amount_usd=amounts,
        income_category=income_categories,
    )
