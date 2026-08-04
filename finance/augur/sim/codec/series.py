"""Decode the external level series into the run's flat, wire-keyed read model.

The compile path is typed by `LevelSeriesKey` end to end (see
`sim/external_series.py`). This is the other end: the decoded frames a caller
joins and serializes, where every entity is already identified by its wire
string — `asset_lots.asset_id`, product wire events. A price frame keyed the
same way is what makes `run.asset_lots ⋈ run.series_values` a join rather than
a lookup table, so the wire id here is the serialization boundary doing its
job, not a shim around a missing type.
"""

from __future__ import annotations

import polars as pl

from finance.augur.frames import FrameSpec, concat_frames
from finance.augur.model.exogenous import LevelFrames

SERIES_VALUES_SCHEMA = pl.Schema(
    {"rollout_index": pl.Int64(), "month_index": pl.Int64(), "series_id": pl.Utf8(), "value": pl.Float64()}
)
SERIES_VALUES_FRAME = FrameSpec("series_values", SERIES_VALUES_SCHEMA)


def decode_series_values(levels: LevelFrames) -> pl.DataFrame:
    """Flatten the typed per-kind frames into `(rollout, month, series_id, value)` rows."""

    return concat_frames(
        [
            frame.with_columns(pl.lit(key.wire_id, dtype=pl.Utf8()).alias("series_id")).select(
                SERIES_VALUES_SCHEMA.names()
            )
            for key, frame in levels.value_rows()
        ],
        SERIES_VALUES_SCHEMA,
    )
