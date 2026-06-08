"""Primary-residence event decoder."""

from __future__ import annotations

import numpy as np
import polars as pl

from finance.augur.sim.buffers import SimulationBuffers
from finance.augur.sim.codec.helpers import codes_to_strings, frame_from_columns
from finance.augur.sim.compiler import CompiledSimulation
from finance.augur.sim.events import EVENT_FRAMES


def decode_primary_residence_events(plan: CompiledSimulation, buffers: SimulationBuffers) -> pl.DataFrame:
    event_count = int(plan.primary_residence_events.month.shape[0])
    if event_count == 0:
        return EVENT_FRAMES.set_primary_residence_events.empty()
    fired = buffers.primary_residence.fired[:event_count]
    events_idx, rollouts = np.argwhere(fired).T if fired.any() else (np.array([], dtype=np.int64),) * 2
    if events_idx.size == 0:
        return EVENT_FRAMES.set_primary_residence_events.empty()

    agent_slots = plan.primary_residence_events.agent_slot.astype(np.int64)[events_idx]
    property_slots = plan.primary_residence_events.property_slot.astype(np.int64)[events_idx]
    property_codes = np.full(property_slots.shape, -1, dtype=np.int64)
    assigned = property_slots >= 0
    property_codes[assigned] = plan.properties.id[property_slots[assigned]]
    return frame_from_columns(
        EVENT_FRAMES.set_primary_residence_events,
        rollout_index=rollouts,
        month_index=plan.primary_residence_events.month.astype(np.int64)[events_idx],
        agent_id=codes_to_strings(plan, plan.agent_codes[agent_slots]),
        property_id=codes_to_strings(plan, property_codes),
        is_primary_residence=property_slots >= 0,
    )
