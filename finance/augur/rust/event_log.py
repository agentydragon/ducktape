"""Read the Rust simulator's canonical event frames into an `EventLog`.

The Rust engine emits these frames in Augur's own column names and units
(`finance/augur/rust/event_frames.rs`), so there is no translation here — only the check
that what arrived is what `events.py` declares. A field renamed or rescaled on one side
therefore fails as a named mismatch rather than as a wrong number nobody attributes.
"""

from collections.abc import Mapping
from typing import Any

import polars as pl

from finance.augur.frames import FrameSpec
from finance.augur.sim.events import EVENT_FRAME_SPECS, EventLog


def decode_event_log(output: Mapping[str, Any]) -> EventLog:
    """Return canonical event frames from a Rust run that retained monthly state."""

    frames = output["event_frames"]
    expected = {spec.name for spec in EVENT_FRAME_SPECS}
    if set(frames) != expected:
        raise ValueError(
            f"Rust emitted event frames {sorted(set(frames) - expected)} and omitted {sorted(expected - set(frames))}"
        )
    return EventLog.from_frames({spec.name: _frame(spec, frames[spec.name]) for spec in EVENT_FRAME_SPECS})


def _frame(spec: FrameSpec, rows: list[dict[str, Any]]) -> pl.DataFrame:
    """One frame, checked column-for-column against its schema.

    Polars fills a column the rows never mention with nulls and ignores a key the schema
    does not name, so without this an added or renamed Rust field reads as an all-null
    column rather than as an error.
    """

    if not rows:
        return spec.empty()
    declared = set(spec.schema.names())
    present = set(rows[0])
    if present != declared:
        raise ValueError(
            f"event frame {spec.name!r}: Rust emitted {sorted(present - declared)} "
            f"and omitted {sorted(declared - present)}"
        )
    return pl.DataFrame(rows, schema=spec.schema)
