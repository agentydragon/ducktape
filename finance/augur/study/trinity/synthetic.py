"""Generated placeholder history for offline study plumbing, not a forecast or evidence."""

from datetime import date

import numpy as np

from finance.augur.model.historical_windows import MacroHistory


def synthetic_history(horizon_months: int) -> MacroHistory:
    """Four overlapping windows with changing prices, yields and CPI."""
    index = np.arange(horizon_months + 4)
    return MacroHistory(
        months=tuple(date(1930 + int(i) // 12, int(i) % 12 + 1, 1) for i in index),
        short_rate=0.01 + index * 0.0001,
        term_spread=np.full(len(index), 0.02),
        corporate_aaa_yield=0.04 + index * 0.0001,
        corporate_baa_yield=0.05 + index * 0.0001,
        equity_level=100.0 * np.exp(np.cumsum(0.001 + index * 0.00001)),
        cpi_level=100.0 * np.exp(np.cumsum(0.001 + index * 0.000003)),
    )
