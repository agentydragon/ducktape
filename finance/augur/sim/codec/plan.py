"""Top-level codec orchestrator: `SimulationRun` is a lazy facade over the
per-domain decoders, producing each long-form Polars frame from a
(plan, buffers, external_series) triple on first access. The triple is exposed
as public attributes for callers (product metrics, profiling) that read the raw
dense buffers directly instead of the decoded frames."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property

import numpy as np
import polars as pl

from finance.augur.sim.buffers import SimulationBuffers
from finance.augur.sim.codec.assets import (
    decode_asset_lots,
    decode_cash,
    decode_liquidity_dispositions,
    decode_pe_dispositions,
    decode_pe_opportunity_events,
    decode_pe_protocol_events,
    decode_sched_dispositions,
)
from finance.augur.sim.codec.liabilities import (
    decode_liabilities,
    decode_mortgage_originations,
    decode_mortgage_payments,
)
from finance.augur.sim.codec.lifecycle import decode_lifecycle_events
from finance.augur.sim.codec.obligations import decode_obligations
from finance.augur.sim.codec.primary_residence import decode_primary_residence_events
from finance.augur.sim.codec.properties import decode_property_purchases, decode_property_stakes, decode_property_state
from finance.augur.sim.codec.tax import (
    decode_capital_gains,
    decode_ordinary_income,
    decode_tax_accruals,
    decode_tax_liabilities,
    decode_tax_settlements,
)
from finance.augur.sim.codec.transfers import decode_property_cashflows, decode_transfers
from finance.augur.sim.compiler import CompiledSimulation
from finance.augur.sim.events import EVENT_FRAMES, EventLog
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.state import ROLLOUT_STATUS_FRAME


# eq=False: a run is a handle around its (plan, buffers, external_series) triple, so identity
# equality is what callers want; field-wise __eq__/__hash__ would also choke on the numpy arrays
# inside `buffers`. frozen so the triple can't be swapped out from under the cached frames.
@dataclass(frozen=True, eq=False)
class SimulationRun:
    """Lazy view of a simulation's outputs: each long-form Polars frame (and the event log) is
    decoded from the dense buffers on first access and cached, so a caller only pays to materialize
    the frames it actually reads. The `(plan, buffers, external_series)` triple is public for callers
    that read the raw dense buffers directly."""

    plan: CompiledSimulation
    buffers: SimulationBuffers
    external_series: ExternalSeriesContext

    @cached_property
    def cash_balances(self) -> pl.DataFrame:
        return decode_cash(self.plan, self.buffers)

    @cached_property
    def asset_lots(self) -> pl.DataFrame:
        return decode_asset_lots(self.plan, self.buffers)

    @cached_property
    def ordinary_income_ytd(self) -> pl.DataFrame:
        return decode_ordinary_income(self.plan, self.buffers)

    @cached_property
    def capital_gains_ytd(self) -> pl.DataFrame:
        return decode_capital_gains(self.plan, self.buffers)

    @cached_property
    def tax_liabilities(self) -> pl.DataFrame:
        return decode_tax_liabilities(self.plan, self.buffers)

    @cached_property
    def property_state(self) -> pl.DataFrame:
        return decode_property_state(self.plan, self.buffers)

    @cached_property
    def property_stakes(self) -> pl.DataFrame:
        return decode_property_stakes(self.plan, self.buffers)

    @cached_property
    def liabilities(self) -> pl.DataFrame:
        return decode_liabilities(self.plan, self.buffers)

    @cached_property
    def rollout_status_history(self) -> pl.DataFrame:
        return decode_rollout_status_history(self.plan, self.buffers)

    @cached_property
    def rollout_status(self) -> pl.DataFrame:
        return decode_final_rollout_status(self.plan, self.buffers)

    @cached_property
    def series_values(self) -> pl.DataFrame:
        return self.external_series.series_values

    @cached_property
    def events_log(self) -> EventLog:
        return decode_events(self.plan, self.buffers)


def decode_events(plan: CompiledSimulation, buffers: SimulationBuffers) -> EventLog:
    transfer_frames: list[pl.DataFrame] = []
    lot_frames: list[pl.DataFrame] = []
    transfer_frames.append(decode_transfers(plan, buffers))
    transfer_frames.append(decode_property_cashflows(plan, buffers))
    property_purchases_frame, property_transfer_frame = decode_property_purchases(plan, buffers)
    transfer_frames.append(property_transfer_frame)
    lot_frames.append(decode_sched_dispositions(plan, buffers))
    lot_frames.append(decode_liquidity_dispositions(plan, buffers))
    lot_frames.append(decode_pe_dispositions(plan, buffers))
    tax_accruals_frame, tax_breakdowns_frame = decode_tax_accruals(plan, buffers)
    obligation_accruals_frame, obligation_settlements_frame, obligation_transfer_frame, failure_frame = (
        decode_obligations(plan, buffers)
    )
    transfer_frames.append(obligation_transfer_frame)
    set_rented_fraction_frame, capital_improvement_frame, property_sale_frame = decode_lifecycle_events(plan, buffers)
    return EventLog.from_frames(
        {
            "transfers": EVENT_FRAMES.transfers.concat(transfer_frames),
            "lot_dispositions": EVENT_FRAMES.lot_dispositions.concat(lot_frames),
            "tax_accruals": tax_accruals_frame,
            "tax_breakdowns": tax_breakdowns_frame,
            "tax_settlements": decode_tax_settlements(plan, buffers),
            "obligation_accruals": obligation_accruals_frame,
            "obligation_settlements": obligation_settlements_frame,
            "property_purchases": property_purchases_frame,
            "mortgage_originations": decode_mortgage_originations(plan, buffers),
            "mortgage_payments": decode_mortgage_payments(plan, buffers),
            "rollout_failures": failure_frame,
            "set_rented_fraction_events": set_rented_fraction_frame,
            "set_primary_residence_events": decode_primary_residence_events(plan, buffers),
            "capital_improvement_events": capital_improvement_frame,
            "property_sale_events": property_sale_frame,
            "private_equity_events": decode_pe_protocol_events(plan),
            "private_equity_opportunities": decode_pe_opportunity_events(plan, buffers),
        }
    )


# Status categories indexed by the rollout's `failed` flag (0 = active, 1 = failed).
_ROLLOUT_STATUS_CATEGORIES = pl.Series("status", ["active", "failed_insufficient_cash"], dtype=pl.Utf8())


def _status_series(failed: np.ndarray) -> pl.Series:
    """Vectorized `failed`-flag → status string via Arrow take (avoids per-element `new_str`)."""
    return _ROLLOUT_STATUS_CATEGORIES.gather(failed.astype(np.int64)).rename("status")


def _failed_month_series(failed_month: np.ndarray) -> pl.Series:
    """`failed_month` int array → Int64 column with NO_CODE (-1) mapped to null, no Python loop."""
    values = failed_month.astype(np.int64)
    return pl.Series("failed_month", values).set(pl.Series(values < 0), None)


def decode_rollout_status_history(plan: CompiledSimulation, buffers: SimulationBuffers) -> pl.DataFrame:
    failed_state = buffers.state.rollout_failed_state  # (H+1, r) bool
    failed_month_state = buffers.state.rollout_failed_month_state  # (H+1, r) int
    h1, r = failed_state.shape
    months = np.broadcast_to(np.arange(h1, dtype=np.int64)[:, None], (h1, r)).ravel()
    rollouts = np.broadcast_to(np.arange(r, dtype=np.int64)[None, :], (h1, r)).ravel()
    return pl.DataFrame(
        {
            "rollout_index": rollouts,
            "month_index": months,
            "status": _status_series(failed_state.reshape(-1)),
            "failed_month": _failed_month_series(failed_month_state.reshape(-1)),
        }
    )


def decode_final_rollout_status(plan: CompiledSimulation, buffers: SimulationBuffers) -> pl.DataFrame:
    month = plan.horizon_months
    failed = buffers.state.rollout_failed_state[month]  # (r,) bool
    r = failed.shape[0]
    if r == 0:
        return ROLLOUT_STATUS_FRAME.empty()
    return ROLLOUT_STATUS_FRAME.normalize(
        pl.DataFrame(
            {
                "rollout_index": np.arange(r, dtype=np.int64),
                "status": _status_series(failed),
                "failed_month": _failed_month_series(buffers.state.rollout_failed_month_state[month]),
            }
        )
    )
