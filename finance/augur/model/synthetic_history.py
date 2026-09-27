"""Deterministic placeholder macro record for replay tests: a known shape, not evidence."""

from __future__ import annotations

from datetime import date

import numpy as np

from finance.augur.model.historical_windows import MacroHistory


def synthetic_history(months: int) -> MacroHistory:
    """A record from 1970-01 whose every series strictly increases, so a window's identity is
    visible in its values: window `i` starts at exactly the month-`i` level of each series."""

    index = np.arange(months, dtype=np.float64)
    # Equity and CPI grow at an ACCELERATING rate, not a constant one. A constant-growth series
    # is shape-invariant under rebasing, so every window would replay identically and the tests
    # that distinguish windows would pass against a sampler that always returned window zero.
    return MacroHistory(
        months=tuple(date(1970 + month // 12, month % 12 + 1, 1) for month in range(months)),
        short_rate=0.01 + index * 0.0001,
        term_spread=0.005 + index * 0.00001,
        # Corporate curves sit above the government one and are distinguishable from it and
        # from each other, so a test can tell which curve an instrument actually priced off.
        corporate_aaa_yield=0.02 + index * 0.0001,
        corporate_baa_yield=0.03 + index * 0.0001,
        equity_level=100.0 * np.exp(np.cumsum(0.004 + index * 0.00001)),
        cpi_level=100.0 * np.exp(np.cumsum(0.0015 + index * 0.000003)),
    )
