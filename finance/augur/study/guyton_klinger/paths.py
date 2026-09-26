"""January-start windows over an annual panel, each composed into one `World`.

Annual-only embedding, not a claimed monthly market path: prices and CPI hold within
each year and the year's move lands at month 12, 24, …; the terminal `12 * years` mark
carries the final year's return. Every sleeve, cash included, is a tax-free total-return
proxy unit, so cash earns its return; checking only settles sales and consumption.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

import numpy as np

from finance.augur.model.series import InflationKey, LevelSeriesKey, SecurityKey, SecuritySymbol
from finance.augur.sim.books import AccountRef
from finance.augur.sim.compiler.execution import compile_series
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.fixed_point import currency_amount_to_quanta, quantity_to_quanta, round_currency_amount
from finance.augur.sim.ids import AccountId, AgentId, AssetId, LotId
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.prepared import PreparedAccount, PreparedHoldingPool, PreparedLot, PreparedSeries
from finance.augur.sim.world import World
from finance.augur.study.guyton_klinger.panel import AnnualPanel, Sleeve

MONTHS_PER_YEAR = 12
# Every sleeve's unit is priced at one dollar in each window's January. Fine enough that
# proxy rounding stays far below any reported digit: micro-dollar money, nano-unit quantities.
QUANTUM = Decimal("0.000001")
QUANTITY_SCALE = 10**9

RETIREE = AgentId("retiree")
WORLD = AgentId("world")
BROKERAGE = AccountId("brokerage")
CHECKING = AccountId("checking")

ADAPTATION_TARGET_PERCENT = {Sleeve.CASH: 10, Sleeve.BONDS: 25, Sleeve.EQUITY: 65}
"""GK2006 Table 1's 65%-equity column with its six equity sleeves merged into one."""


@dataclass(frozen=True)
class AnnualWindows:
    """Rollout ID `i` is the window starting in January of `start_years[i]`."""

    start_years: tuple[int, ...]
    years: int
    series: tuple[PreparedSeries, ...]

    @property
    def horizon_months(self) -> int:
        return self.years * MONTHS_PER_YEAR


def eligible_start_years(panel: AnnualPanel, years: int) -> tuple[int, ...]:
    """Every January start whose `years` complete years the panel covers."""
    if years < 1:
        raise ValueError(f"{years=} must be positive")
    return tuple(panel.years[: max(0, len(panel.years) - years + 1)])


def _levels(returns: Sequence[float], start_index: int, years: int) -> np.ndarray:
    growth = np.cumprod([1.0, *(1.0 + np.asarray(returns[start_index : start_index + years]))])
    return np.repeat(growth, [*([MONTHS_PER_YEAR] * years), 1])


def annual_windows(panel: AnnualPanel, *, start_years: Sequence[int], years: int) -> AnnualWindows:
    """Materialize the selected windows once; no imputation, every window complete."""
    if not start_years or len(set(start_years)) != len(start_years):
        raise ValueError(f"{start_years=} must be nonempty and distinct")
    eligible = eligible_start_years(panel, years)
    missing = [year for year in start_years if year not in eligible]
    if missing:
        raise ValueError(f"panel {panel.years} lacks {years} complete years from {missing}")
    offsets = [year - panel.first_year for year in start_years]
    blocks: list[tuple[LevelSeriesKey, np.ndarray]] = [
        (
            SecurityKey(symbol=SecuritySymbol(sleeve)),
            np.stack([_levels(panel.returns[sleeve], offset, years) for offset in offsets]),
        )
        for sleeve in Sleeve
    ]
    blocks.append((InflationKey(), np.stack([_levels(panel.inflation, offset, years) for offset in offsets])))
    horizon = years * MONTHS_PER_YEAR
    return AnnualWindows(
        start_years=tuple(start_years),
        years=years,
        series=compile_series(
            ExternalSeriesContext.from_level_blocks(blocks, rollout_count=len(offsets), horizon_months=horizon),
            rollout_count=len(offsets),
            horizon_months=horizon,
            currency_quantum=QUANTUM,
        ),
    )


def compose_world(windows: AnnualWindows, rollout_id: int, *, wealth: Decimal, weights: Mapping[Sleeve, int]) -> World:
    """One window's books: `wealth` split by `weights` into sleeve lots, empty checking.

    Every sleeve's pool is declared, held or not. No tax vocabulary is declared: the
    paper control has no investor taxes or trading fees.
    """
    total = sum(weights.values())
    if set(weights) != set(Sleeve) or min(weights.values()) < 0 or total <= 0:
        raise ValueError(f"{weights=} must weigh every sleeve, nonnegatively, with a positive total")
    world = World(
        MarketPath(windows.series, rollout_id, rollout_count=len(windows.start_years)),
        horizon_months=windows.horizon_months,
    )
    for agent_id in (RETIREE, WORLD):
        world.declare_account(
            PreparedAccount(account=AccountRef(agent_id=agent_id, account_id=CHECKING), opening_balance=0)
        )
    for sleeve in Sleeve:
        world.declare_pool(
            PreparedHoldingPool(
                agent_id=RETIREE, account_id=BROKERAGE, asset_id=AssetId(sleeve), quantity_scale=QUANTITY_SCALE
            )
        )
    for sleeve in Sleeve:
        if not weights[sleeve]:
            continue
        value = round_currency_amount(wealth * weights[sleeve] / total, quantum=QUANTUM)
        world.hold(
            PreparedLot(
                lot_id=LotId(f"{sleeve}_opening"),
                agent_id=RETIREE,
                account_id=BROKERAGE,
                asset_id=AssetId(sleeve),
                purchase_month=-1,
                quantity_scale=QUANTITY_SCALE,
                units=int(quantity_to_quanta(value, scale=QUANTITY_SCALE)),
                basis=int(currency_amount_to_quanta(value, quantum=QUANTUM)),
            )
        )
    return world
