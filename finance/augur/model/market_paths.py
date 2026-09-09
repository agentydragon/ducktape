"""Materialized public-market inputs, reusable across product constructions.

This is the supported short-rate/spread, CPI and broad-equity path vocabulary,
not a protocol every forecast model must implement. Arrays are decoded NumPy
values shaped `(rollout, horizon_months + 1)`, including the opening observation.
"""

from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np

from finance.augur.model.bond_fund import YieldCurve


@dataclass(frozen=True)
class MarketPaths:
    """No security prices, payouts or investor choices; rates are annualized decimals.

    The equity index is total return, normalized to one at opening. Corporate
    yields are present only when observed/modeled, never filled from Treasuries.
    Short rates retain the source values before product-construction guards.
    Provenance identifies the source paths, including historical dates or seeds.
    """

    short_rate: np.ndarray
    term_spread: np.ndarray
    cpi_level: np.ndarray
    equity_total_return_index: np.ndarray | None
    corporate_yields: Mapping[YieldCurve, np.ndarray]
    model_id: str
    provenance: Mapping[str, object]

    def __post_init__(self) -> None:
        shape = self.short_rate.shape
        if len(shape) != 2 or shape[0] == 0 or shape[1] == 0:
            raise ValueError("market paths need (rollout, month) arrays including the opening observation")
        arrays = [self.term_spread, self.cpi_level, *self.corporate_yields.values()]
        if self.equity_total_return_index is not None:
            arrays.append(self.equity_total_return_index)
        if any(array.shape != shape for array in arrays):
            raise ValueError("market path arrays must share their rollout/month axes")
        if any(not np.all(np.isfinite(array)) for array in (self.short_rate, *arrays)):
            raise ValueError("market paths must contain only finite values")
        if np.any(self.cpi_level <= 0.0):
            raise ValueError("CPI levels must be strictly positive")
        if self.equity_total_return_index is not None and np.any(self.equity_total_return_index <= 0.0):
            raise ValueError("equity total-return indices must be strictly positive")
        if self.equity_total_return_index is not None and np.any(self.equity_total_return_index[:, 0] != 1.0):
            raise ValueError("equity total-return indices must start at one")
        if YieldCurve.GOVERNMENT in self.corporate_yields:
            raise ValueError("corporate_yields must not override the government rate/spread path")

    @property
    def rollout_count(self) -> int:
        return int(self.short_rate.shape[0])

    @property
    def horizon_months(self) -> int:
        return int(self.short_rate.shape[1]) - 1
