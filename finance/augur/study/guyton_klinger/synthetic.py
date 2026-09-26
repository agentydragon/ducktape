"""Generated placeholder panel for offline study plumbing, not a forecast or evidence."""

import numpy as np

from finance.augur.study.guyton_klinger.panel import AnnualPanel, Sleeve


def synthetic_panel(years: int) -> AnnualPanel:
    """`years + 3` calendar years from 1930: four complete windows of `years`, with losing equity years."""
    index = np.arange(years + 3)
    return AnnualPanel(
        first_year=1930,
        income={
            Sleeve.CASH: tuple((0.02 + 0.001 * index).tolist()),
            Sleeve.BONDS: tuple((0.04 + 0.005 * np.cos(index)).tolist()),
            Sleeve.EQUITY: tuple((0.03 + 0.005 * np.sin(0.7 * index)).tolist()),
        },
        price={
            Sleeve.BONDS: tuple((0.03 * np.sin(index)).tolist()),
            Sleeve.EQUITY: tuple((0.04 + 0.2 * np.sin(1.3 * index)).tolist()),
        },
        inflation=tuple((0.025 + 0.01 * np.cos(index)).tolist()),
    )
