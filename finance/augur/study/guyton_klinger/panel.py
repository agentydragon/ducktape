"""The annual evidence the Guyton-Klinger replay runs on: each sleeve's income and price returns, and CPI change.

File format: CSV with exactly the header `HEADER`, one row per consecutive calendar year in
ascending order, every cell a fraction (`0.05` is 5%). A sleeve's income is what it paid over the
year per dollar of its value at the year's start: the equity dividend over the prior year-end index
level, the bond coupon at the prior year-end yield, the bill interest. Its price return is the
change in its unit price, so income plus price return is its total return. Bills hold their price
and pay all their return as interest, so cash has no price column. Income is nonnegative, a price
return and `inflation` (that year's CPI change) exceed -1, and no cell is blank or non-finite. The
source and its calendar transformation are pinned beside the file, not in it.
"""

import csv
import math
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

INFLATION = "inflation"
YEAR = "year"


class Sleeve(StrEnum):
    """The declared three-sleeve adaptation; each value is its column prefix and security symbol."""

    CASH = "cash"
    BONDS = "bonds"
    EQUITY = "equity"


PRICED = (Sleeve.BONDS, Sleeve.EQUITY)
"""The sleeves whose unit price moves; cash pays all its return as interest."""


def income_column(sleeve: Sleeve) -> str:
    return f"{sleeve}_income"


def price_column(sleeve: Sleeve) -> str:
    return f"{sleeve}_price"


HEADER = (
    YEAR,
    income_column(Sleeve.CASH),
    *(column for sleeve in PRICED for column in (income_column(sleeve), price_column(sleeve))),
    INFLATION,
)


@dataclass(frozen=True)
class AnnualPanel:
    """Calendar-year returns from `first_year` on, one entry per year in every column."""

    first_year: int
    income: dict[Sleeve, tuple[float, ...]]
    # `PRICED` sleeves only.
    price: dict[Sleeve, tuple[float, ...]]
    inflation: tuple[float, ...]

    def __post_init__(self) -> None:
        if set(self.income) != set(Sleeve) or set(self.price) != set(PRICED):
            raise ValueError(f"panel needs income for every sleeve and prices for {PRICED}")
        if not self.inflation:
            raise ValueError("panel has no years")
        for sleeve, values in self.income.items():
            self._check(income_column(sleeve), values, lambda value: value >= 0, "of at least 0")
        for column, values in (
            *((price_column(sleeve), values) for sleeve, values in self.price.items()),
            (INFLATION, self.inflation),
        ):
            self._check(column, values, lambda value: value > -1, "above -1")

    def _check(self, column: str, values: tuple[float, ...], valid: Callable[[float], bool], need: str) -> None:
        if len(values) != len(self.inflation):
            raise ValueError(f"{column=} covers {len(values)} years, not {len(self.inflation)}")
        for year, value in zip(self.years, values, strict=True):
            if not (math.isfinite(value) and valid(value)):
                raise ValueError(f"{column=} {year=} has {value=}; need a finite value {need}")

    @property
    def years(self) -> range:
        return range(self.first_year, self.first_year + len(self.inflation))

    def total_return(self, sleeve: Sleeve) -> tuple[float, ...]:
        if sleeve not in self.price:
            return self.income[sleeve]
        return tuple(income + price for income, price in zip(self.income[sleeve], self.price[sleeve], strict=True))


def load_panel(path: Path) -> AnnualPanel:
    with path.open(newline="") as handle:
        rows = list(csv.reader(handle))
    if not rows or tuple(rows[0]) != HEADER:
        raise ValueError(f"{path}: header must be {','.join(HEADER)}")
    body = rows[1:]
    if not body or any(len(row) != len(HEADER) for row in body):
        raise ValueError(f"{path}: need at least one year, each row with {len(HEADER)} cells")
    years = [int(row[0]) for row in body]
    if years != list(range(years[0], years[0] + len(years))):
        raise ValueError(f"{path}: years must be consecutive and ascending; got {years}")
    columns = {
        name: tuple(float(value) for value in values)
        for name, values in zip(HEADER, zip(*body, strict=True), strict=True)
    }
    return AnnualPanel(
        first_year=years[0],
        income={sleeve: columns[income_column(sleeve)] for sleeve in Sleeve},
        price={sleeve: columns[price_column(sleeve)] for sleeve in PRICED},
        inflation=columns[INFLATION],
    )
