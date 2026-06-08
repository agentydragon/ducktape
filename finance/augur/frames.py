"""Shared helpers for typed Polars frames."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import polars as pl


@dataclass(frozen=True)
class FrameSpec:
    """One named Polars relation and its schema."""

    name: str
    schema: pl.Schema

    def empty(self) -> pl.DataFrame:
        return self.schema.to_frame()

    def normalize(self, frame: pl.DataFrame) -> pl.DataFrame:
        return frame.select(self.schema.names())

    def concat(self, frames: Iterable[pl.DataFrame]) -> pl.DataFrame:
        return concat_frames(frames, self.schema)


def concat_frames(frames: Iterable[pl.DataFrame], schema: pl.Schema) -> pl.DataFrame:
    """Concatenate frames while preserving the typed empty case."""

    return pl.concat([schema.to_frame(), *frames]).select(schema.names())
