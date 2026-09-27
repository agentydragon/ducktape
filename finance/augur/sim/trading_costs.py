"""Declared proportional trading costs: what each executed Buy and Sell of a public security pays.

A trade pays its security's declared rate on the gross value of each lot it buys or sells (units
at the month's price), rounded to the nearest currency quantum with exact ties away from zero;
its cost is the sum over those lots. The trade's own cash account pays it (a buy's cash account,
a sale's proceeds account) in a journal entry of its own beside the trade's, never netted into
the price. A buy whose cash cannot fund the cost as well is rejected like any other unfunded
purchase; a sale's cost, at most its gross value, comes out of its proceeds.

Tax treatment follows US practice: a buy's cost is a cost of purchase, so it joins the lot's
basis (IRS Publication 551); a sale's is a selling expense, so each lot's proceeds and realized
gain are net of that lot's cost (the net proceeds of the Form 8949 instructions).

A world that declares no schedule models no trading costs. Its books settle as a declared zero
rate's would, but its receipts and results read `None`, not modeled, where a declared zero reads
0. Managed-portfolio contributions and redemptions and issuer-driven private-equity sales are not
trades a schedule prices.
"""

from finance.augur.sim.books import TradingCostOutcome
from finance.augur.sim.fixed_point import MONEY_FACTOR_SCALE
from finance.augur.sim.money import mul_div
from finance.augur.sim.prepared import PreparedTradingCosts


class TradingCosts:
    """The declared schedule and the costs this month's trades paid under it."""

    def __init__(self, schedule: PreparedTradingCosts) -> None:
        self.schedule = schedule
        # This month's charges, cleared by `begin_month`.
        self.paid: list[TradingCostOutcome] = []

    def begin_month(self) -> None:
        self.paid.clear()


def trading_cost(gross: int, rate_ppb: int) -> int:
    """`rate_ppb` parts per billion of `gross`, to the nearest quantum with exact ties away from zero."""
    return mul_div(gross, rate_ppb, MONEY_FACTOR_SCALE, "trading cost")
