"""Transfer domain decoder. The compile-side twin is `TransferCompileOutput` +
`_compile_transfers` in `augur.sim.compiler`."""

from __future__ import annotations

import numpy as np
import polars as pl

from augur.sim.buffers import SimulationBuffers
from augur.sim.codec.helpers import code_column, frame_from_columns
from augur.sim.compiler import CompiledSimulation
from augur.sim.events import EVENT_FRAMES


def decode_transfers(plan: CompiledSimulation, buffers: SimulationBuffers) -> pl.DataFrame:
    active = buffers.transfers.active  # (M, S, R)
    months, slots, rollouts = np.argwhere(active).T if active.any() else (np.array([], dtype=np.int64),) * 3
    amounts = buffers.transfers.amount[months, slots, rollouts]
    income_categories = np.full(len(months), None, dtype=object)
    income_categories[plan.transfers.income_profile[months, slots] >= 0] = "ordinary"
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
