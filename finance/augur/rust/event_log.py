"""Read the Rust simulator's canonical event frames into an `EventLog`.

The Rust engine emits these frames in Augur's own column names and units
(`finance/augur/rust/event_frames.rs`), so there is no translation here — only the check
that what arrived is what `events.py` declares. A field renamed or rescaled on one side
therefore fails as a named mismatch rather than as a wrong number nobody attributes.
"""

from collections.abc import Mapping
from typing import Any

from finance.augur.sim.events import EventLog


def decode_event_log(output: Mapping[str, Any]) -> EventLog:
    """Return canonical event frames from a Rust run that retained monthly state."""

    # Full configured output and a selected action trace carry their own source IDs.
    # Read these even when every event frame is empty, never infer owners from rows.
    ids = (
        [rollout["rollout_id"] for rollout in output["rollouts"]]
        if "rollouts" in output
        else [output["financial"]["rollout_id"]]
    )
    return EventLog.from_serialized(output["event_frames"], rollout_ids=ids)
