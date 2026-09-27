from __future__ import annotations

from decimal import Decimal

from finance.augur.api.schemas import ApiModel
from finance.augur.sim.scenario import NonNegativeCurrencyAmount


class FinanceSnapshot(ApiModel):
    """Minimal initial balance snapshot consumed at scenario-build time.

    Per-holding detail (lots, cost basis, security kind) lives in `PortfolioConfig`;
    this struct only carries the cash balance and the snapshot date the product
    portfolio response surfaces to the UI.
    """

    as_of_date: str
    cash: NonNegativeCurrencyAmount = Decimal(0)
