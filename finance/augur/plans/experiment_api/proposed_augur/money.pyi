"""Currency amounts and purchasing-power units shared by finance and policies.

Owns exact ledger money and explicit real-budget bases. Numeric policy calculations
are permitted, but must preserve their currency, base date, index and path keys.
Does not forecast inflation, value products or choose a spending budget.
"""

from dataclasses import dataclass
from datetime import date

import numpy as np
from numpy.typing import NDArray

type FloatArray = NDArray[np.float64]

type BoolArray = NDArray[np.bool_]

@dataclass(frozen=True)
class PriceIndex:
    name: str

@dataclass(frozen=True)
class ReportingBasis:
    currency: str
    price_index: PriceIndex
    base_date: date

class Money:
    @classmethod
    def from_number(cls, value: float, *, currency: str) -> Money: ...
    def __add__(self, other: Money) -> Money: ...
    def __sub__(self, other: Money) -> Money: ...
    def __lt__(self, other: Money) -> bool: ...
    def __mul__(self, value: float) -> Money: ...
    def __truediv__(self, value: float) -> Money: ...
    def to_number(self) -> float: ...

class USD(Money):
    def __init__(self, value: str) -> None: ...

class GBP(Money):
    def __init__(self, value: str) -> None: ...

@dataclass(frozen=True)
class RealAmount:
    value: float
    basis: ReportingBasis
    @classmethod
    def at_base(cls, amount: Money, basis: ReportingBasis) -> RealAmount: ...
