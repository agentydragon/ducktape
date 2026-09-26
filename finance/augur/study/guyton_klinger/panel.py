"""The annual evidence the Guyton-Klinger replay runs on: three sleeves' total returns and CPI change.

File format: CSV with exactly the header `year,cash,bonds,equity,inflation`, one row per
consecutive calendar year in ascending order. Each sleeve column is that year's total
return and `inflation` that year's CPI change, as fractions (`0.05` is 5%). A return must
exceed -1; no cell may be blank or non-finite. The source and its calendar
transformation are pinned beside the file, not in it.
"""

import csv
import math
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

INFLATION = "inflation"
YEAR = "year"


class Sleeve(StrEnum):
    """The declared three-sleeve adaptation; each value is its panel column and security symbol."""

    CASH = "cash"
    BONDS = "bonds"
    EQUITY = "equity"


@dataclass(frozen=True)
class AnnualPanel:
    """Calendar-year returns from `first_year` on, one entry per year in every column."""

    first_year: int
    returns: dict[Sleeve, tuple[float, ...]]
    inflation: tuple[float, ...]

    def __post_init__(self) -> None:
        if set(self.returns) != set(Sleeve):
            raise ValueError(f"panel needs returns for every sleeve; got {sorted(self.returns)}")
        if not self.inflation:
            raise ValueError("panel has no years")
        for column, values in (*self.returns.items(), (INFLATION, self.inflation)):
            if len(values) != len(self.inflation):
                raise ValueError(f"{column=} covers {len(values)} years, not {len(self.inflation)}")
            for year, value in zip(self.years, values, strict=True):
                if not math.isfinite(value) or value <= -1:
                    raise ValueError(f"{column=} {year=} has {value=}; need a finite return above -1")

    @property
    def years(self) -> range:
        return range(self.first_year, self.first_year + len(self.inflation))


def load_panel(path: Path) -> AnnualPanel:
    with path.open(newline="") as handle:
        rows = list(csv.reader(handle))
    header = [YEAR, *Sleeve, INFLATION]
    if not rows or rows[0] != header:
        raise ValueError(f"{path}: header must be {','.join(header)}")
    body = rows[1:]
    if not body or any(len(row) != len(header) for row in body):
        raise ValueError(f"{path}: need at least one year, each row with {len(header)} cells")
    years = [int(row[0]) for row in body]
    if years != list(range(years[0], years[0] + len(years))):
        raise ValueError(f"{path}: years must be consecutive and ascending; got {years}")
    columns = list(zip(*body, strict=True))
    return AnnualPanel(
        first_year=years[0],
        returns={sleeve: tuple(float(value) for value in columns[1 + index]) for index, sleeve in enumerate(Sleeve)},
        inflation=tuple(float(value) for value in columns[-1]),
    )
