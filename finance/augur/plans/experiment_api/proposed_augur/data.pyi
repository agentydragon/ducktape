"""Author-selected dated series, provider loading and explicit alignment.

Keeps observation dates separate from publication/revision dates. The experiment
names its datasets and transformations; this module does not select a universal
evidence bundle or infer a model's variables. History is the simpler return-record
input for annual studies. Calendar describes simulation time, not model dynamics.
"""

from collections.abc import Callable, Mapping
from datetime import date
from pathlib import Path
from typing import Literal

import polars as pl

type NamedSeries = Mapping[str, Series]

type ObservationsEncoder = Callable[[NamedSeries, date], NamedSeries]

class Series:
    def available_by(self, as_of: date) -> Series: ...
    def log(self) -> Series: ...

def read_parquet_series(path: Path, *, value: str, observed_at: str, released_at: str, revision_at: str) -> Series: ...
def read_fred_series(series_id: str, *, snapshot: Path) -> Series: ...
def align_monthly(
    series: NamedSeries,
    *,
    start: date,
    end: date | None = ...,
    months: int | None = ...,
    include_start: bool = ...,
    missing: Literal["raise"],
) -> pl.DataFrame: ...

class History:
    @classmethod
    def load(cls, path: Path) -> History: ...

class Calendar:
    start: date
