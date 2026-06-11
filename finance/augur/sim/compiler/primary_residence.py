"""Primary-residence assignment compiler.

The scenario-level shape is agent-scoped: one current primary-residence property per agent,
plus sparse events that reassign or clear it over time. The engine reads this state when
incrementing Section 121 qualifying-use months.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from finance.augur.sim.compiler.helpers import NO_CODE
from finance.augur.sim.scenario import Scenario


@dataclass(frozen=True)
class PrimaryResidenceEventCompileOutput:
    """Sparse per-month primary-residence assignment events.

    `property_slot[i] == NO_CODE` clears the agent's assignment. Otherwise it stores the
    property slot assigned as that agent's primary residence from this event month forward.
    """

    month: NDArray[np.int64]
    agent_slot: NDArray[np.int64]
    property_slot: NDArray[np.int64]
    month_starts: NDArray[np.int64]


def compile_primary_residences(
    scenario: Scenario, *, agent_slot_by_id: dict[str, int], property_slot_by_id: dict[str, int]
) -> tuple[NDArray[np.int64], PrimaryResidenceEventCompileOutput]:
    initial = np.full(len(agent_slot_by_id), NO_CODE, dtype=np.int64)
    for assignment in scenario.initial_primary_residences:
        initial[agent_slot_by_id[assignment.agent_id]] = property_slot_by_id[assignment.property_id]

    events_sorted = sorted(scenario.primary_residence_events, key=lambda event: (int(event.month), event.agent_id))
    count = len(events_sorted)
    month = np.empty(count, dtype=np.int64)
    agent_slot = np.empty(count, dtype=np.int64)
    property_slot = np.full(count, NO_CODE, dtype=np.int64)
    for idx, event in enumerate(events_sorted):
        month[idx] = int(event.month)
        agent_slot[idx] = agent_slot_by_id[event.agent_id]
        if event.property_id is not None:
            property_slot[idx] = property_slot_by_id[event.property_id]

    month_starts = np.searchsorted(month, np.arange(int(scenario.horizon_months) + 1), side="left").astype(np.int64)
    return initial, PrimaryResidenceEventCompileOutput(
        month=month, agent_slot=agent_slot, property_slot=property_slot, month_starts=month_starts
    )
