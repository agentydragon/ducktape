"""Simulation handoff and event-log codec.

``SimulationRun`` keeps the compiled plan, dense output, and external-series context
that form the simulator's canonical contract. Event decoding remains available as a
lazy convenience; state histories are read directly from the dense output instead of
being mirrored into long-form Polars frames.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property

import numpy as np
import polars as pl

from finance.augur.sim.codec.assets import (
    decode_pe_dispositions,
    decode_pe_opportunity_events,
    decode_pe_protocol_events,
    decode_sched_dispositions,
    decode_target_allocation_dispositions,
)
from finance.augur.sim.codec.liabilities import decode_mortgage_originations, decode_mortgage_payments
from finance.augur.sim.codec.lifecycle import decode_lifecycle_events
from finance.augur.sim.codec.obligations import decode_obligations
from finance.augur.sim.codec.primary_residence import decode_primary_residence_events
from finance.augur.sim.codec.properties import decode_property_purchases
from finance.augur.sim.codec.tax import decode_tax_accruals, decode_tax_settlements
from finance.augur.sim.codec.transfers import decode_cashflows
from finance.augur.sim.compiler.plan import CompiledSimulation
from finance.augur.sim.events import EVENT_FRAMES, EventLog
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.output import DenseSimulationOutput


@dataclass(frozen=True, eq=False)
class SimulationRun:
    """Handle around the canonical simulation handoff.

    State arrays are intentionally not projected into Polars mirrors. Consumers should
    use ``run.output.state`` together with ``run.plan``; the event log is the only lazy
    decoded read model retained here.
    """

    plan: CompiledSimulation
    output: DenseSimulationOutput
    external_series: ExternalSeriesContext

    @cached_property
    def events_log(self) -> EventLog:
        return decode_events(self.plan, self.output)


def _last_reported_month(plan: CompiledSimulation, output: DenseSimulationOutput) -> pl.DataFrame:
    """The last month each rollout has anything to report.

    A rollout that cannot pay stops at that month: nothing later happens in it. This engine
    cannot leave a vectorized scan early, so it keeps stepping the frozen rollout under a
    mask, and the decoders below would otherwise surface those masked steps as rows — a
    year-end assessment of zero for a tax year the rollout did not survive, exogenous issuer
    marks it was never around to see. Neither is a number that went wrong; they are months
    that did not happen.

    The failure month itself still reports, so that the failure event does.
    """

    failed_month = np.asarray(output.state.failed_month[int(plan.horizon_months)], dtype=np.int64)
    return pl.DataFrame(
        {
            "rollout_index": np.arange(failed_month.size, dtype=np.int64),
            "_last_reported_month": np.where(failed_month < 0, np.int64(plan.horizon_months), failed_month),
        }
    )


def _drop_months_the_rollout_never_reached(frame: pl.DataFrame, last_month: pl.DataFrame) -> pl.DataFrame:
    return (
        frame.join(last_month, on="rollout_index", how="left")
        .filter(pl.col("month_index") <= pl.col("_last_reported_month"))
        .drop("_last_reported_month")
    )


def decode_events(plan: CompiledSimulation, output: DenseSimulationOutput) -> EventLog:
    transfer_frames = [decode_cashflows(plan, output)]
    lot_frames: list[pl.DataFrame] = []
    property_purchases_frame, property_transfer_frame = decode_property_purchases(plan, output)
    transfer_frames.append(property_transfer_frame)
    lot_frames.append(decode_sched_dispositions(plan, output))
    lot_frames.append(decode_target_allocation_dispositions(plan, output))
    lot_frames.append(decode_pe_dispositions(plan, output))
    tax_accruals_frame, tax_breakdowns_frame = decode_tax_accruals(plan, output)
    obligation_accruals_frame, obligation_settlements_frame, obligation_transfer_frame, failure_frame = (
        decode_obligations(plan, output)
    )
    transfer_frames.append(obligation_transfer_frame)
    set_rented_fraction_frame, capital_improvement_frame, property_sale_frame = decode_lifecycle_events(plan, output)
    last_month = _last_reported_month(plan, output)
    return EventLog.from_frames(
        {
            name: _drop_months_the_rollout_never_reached(frame, last_month)
            for name, frame in {
                "transfers": EVENT_FRAMES.transfers.concat(transfer_frames),
                "lot_dispositions": EVENT_FRAMES.lot_dispositions.concat(lot_frames),
                "tax_accruals": tax_accruals_frame,
                "tax_breakdowns": tax_breakdowns_frame,
                "tax_settlements": decode_tax_settlements(plan, output),
                "obligation_accruals": obligation_accruals_frame,
                "obligation_settlements": obligation_settlements_frame,
                "property_purchases": property_purchases_frame,
                "mortgage_originations": decode_mortgage_originations(plan, output),
                "mortgage_payments": decode_mortgage_payments(plan, output),
                "rollout_failures": failure_frame,
                "set_rented_fraction_events": set_rented_fraction_frame,
                "set_primary_residence_events": decode_primary_residence_events(plan, output),
                "capital_improvement_events": capital_improvement_frame,
                "property_sale_events": property_sale_frame,
                "private_equity_events": decode_pe_protocol_events(plan),
                "private_equity_opportunities": decode_pe_opportunity_events(plan, output),
            }.items()
        }
    )
