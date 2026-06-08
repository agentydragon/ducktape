"""Lifecycle event decoder. The compile-side twin is `LifecycleEventCompileOutput`
+ `_compile_lifecycle_events` in `augur.sim.compiler`."""

from __future__ import annotations

import numpy as np
import polars as pl

from finance.augur.sim.buffers import SimulationBuffers
from finance.augur.sim.codec.helpers import codes_to_strings, frame_from_columns, usd_column
from finance.augur.sim.compiler import CompiledSimulation
from finance.augur.sim.enums import LifecycleKind
from finance.augur.sim.events import EVENT_FRAMES


def decode_lifecycle_events(
    plan: CompiledSimulation, buffers: SimulationBuffers
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """Decode `buffers.lifecycle` into per-kind polars frames.

    Each lifecycle event is fanned out to one row per active (rollout, event) pair using
    `buffers.lifecycle.fired`. The compile-time `lifecycle_event_kind` selects which schema
    each event belongs to; sale events additionally pull per-rollout dollar figures from the
    `sale_*` arrays.
    """

    event_count = int(plan.lifecycle_events.month.shape[0])
    if event_count == 0:
        return (
            EVENT_FRAMES.set_rented_fraction_events.empty(),
            EVENT_FRAMES.capital_improvement_events.empty(),
            EVENT_FRAMES.property_sale_events.empty(),
        )
    fired = buffers.lifecycle.fired[:event_count]  # (E, R)
    events_idx, rollouts = np.argwhere(fired).T if fired.any() else (np.array([], dtype=np.int64),) * 2
    if events_idx.size == 0:
        return (
            EVENT_FRAMES.set_rented_fraction_events.empty(),
            EVENT_FRAMES.capital_improvement_events.empty(),
            EVENT_FRAMES.property_sale_events.empty(),
        )
    months = plan.lifecycle_events.month.astype(np.int64)[events_idx]
    property_slots = plan.lifecycle_events.property_slot.astype(np.int64)[events_idx]
    property_ids = codes_to_strings(plan, plan.properties.id)[property_slots]
    kinds = plan.lifecycle_events.kind.astype(np.int64)[events_idx]
    fraction_mask = kinds == LifecycleKind.FRACTION
    capital_mask = kinds == LifecycleKind.CAPITAL_IMPROVEMENT
    sale_mask = kinds == LifecycleKind.SALE

    set_rented_fraction_frame = frame_from_columns(
        EVENT_FRAMES.set_rented_fraction_events,
        rollout_index=rollouts[fraction_mask],
        month_index=months[fraction_mask],
        property_id=property_ids[fraction_mask],
        rented_fraction=plan.lifecycle_events.rented_fraction.astype(np.float64)[events_idx[fraction_mask]],
    )
    capital_improvement_frame = frame_from_columns(
        EVENT_FRAMES.capital_improvement_events,
        rollout_index=rollouts[capital_mask],
        month_index=months[capital_mask],
        property_id=property_ids[capital_mask],
        amount_usd=usd_column(plan.lifecycle_events.amount_cents[events_idx[capital_mask]]),
        description=np.full(int(capital_mask.sum()), "", dtype=object),
    )
    property_sale_frame = frame_from_columns(
        EVENT_FRAMES.property_sale_events,
        rollout_index=rollouts[sale_mask],
        month_index=months[sale_mask],
        property_id=property_ids[sale_mask],
        gross_proceeds_usd=usd_column(
            buffers.lifecycle.sale_gross_proceeds[events_idx[sale_mask], rollouts[sale_mask]]
        ),
        mortgage_payoff_usd=usd_column(
            buffers.lifecycle.sale_mortgage_payoff[events_idx[sale_mask], rollouts[sale_mask]]
        ),
        net_cash_to_owner_usd=usd_column(buffers.lifecycle.sale_net_cash[events_idx[sale_mask], rollouts[sale_mask]]),
        realized_gain_usd=usd_column(buffers.lifecycle.sale_realized_gain[events_idx[sale_mask], rollouts[sale_mask]]),
        depreciation_recapture_usd=usd_column(
            buffers.lifecycle.sale_recapture[events_idx[sale_mask], rollouts[sale_mask]]
        ),
        section_121_exclusion_usd=usd_column(
            buffers.lifecycle.sale_section_121_exclusion[events_idx[sale_mask], rollouts[sale_mask]]
        ),
        long_term_capital_gain_usd=usd_column(
            buffers.lifecycle.sale_long_term_gain[events_idx[sale_mask], rollouts[sale_mask]]
        ),
    )
    return set_rented_fraction_frame, capital_improvement_frame, property_sale_frame
