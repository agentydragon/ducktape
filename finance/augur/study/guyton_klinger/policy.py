"""Guyton-Klinger 2006 four-rule annual policy over one cash, one bond and one equity sleeve.

Runs on the declared adaptation (<README.md>) as <paths.py> composes it: `ADAPTATION_TARGET_PERCENT`,
each sleeve's pool in `BROKERAGE` named by its `Sleeve` value, `CHECKING` only settlement cash, and
withdrawals consumed into `WORLD`'s checking. Reviews fall at months 0, 12, …; the month after a
review records the settled post-withdrawal wealth as the next investment-return denominator, and
no other month trades.

Readings of the source contract's open decisions, recorded with reasons in that doc:

- ORDER: the prior withdrawal is scaled by the preceding year's CPI ratio, deflation included; the
  freeze withholds only an increase; then at most one guardrail is tested on that candidate against
  opening wealth. The adjusted amount is next year's basis, not inflated again this year.
- PORTFOLIO: a sleeve is overweight by its value above its target share of opening wealth, before
  this review's trades, and only if its unit price rose over the preceding year. Funding follows
  `Stage` declaration order; the sweep sells what funding left of that excess into the cash sleeve.
- OPENING: targets cover all opening wealth, with no separate first-withdrawal reserve; year 0 knows
  no returns, so nothing is overweight. Capital preservation applies while the zero-based year index
  is below `years - PRESERVATION_OFF_FINAL_YEARS`.
"""

from dataclasses import dataclass, field, replace
from enum import StrEnum
from fractions import Fraction
from math import ceil

from finance.augur.policy.sleeves import quoted_value, sale_lots
from finance.augur.sim.actions import Action, Buy, Consume, DecisionActions, Sell
from finance.augur.sim.books import AccountRef
from finance.augur.sim.fixed_point import quantity_for_value
from finance.augur.sim.ids import AssetId, LotId
from finance.augur.sim.observations import Decision, Observation, PublicPosition
from finance.augur.study.guyton_klinger.panel import Sleeve
from finance.augur.study.guyton_klinger.paths import (
    ADAPTATION_TARGET_PERCENT,
    BROKERAGE,
    CHECKING,
    MONTHS_PER_YEAR,
    WORLD,
)

PRESERVATION_TRIGGER = Fraction(6, 5)
PROSPERITY_TRIGGER = Fraction(4, 5)
CUT = Fraction(9, 10)
RAISE = Fraction(11, 10)
PRESERVATION_OFF_FINAL_YEARS = 15


class Stage(StrEnum):
    """Withdrawal funding sources; declaration order is funding order."""

    OVERWEIGHT_EQUITY = "overweight_equity"
    OVERWEIGHT_BONDS = "overweight_bonds"
    # Unreserved checking first, then cash-sleeve units.
    CASH = "cash"
    BONDS = "bonds"
    EQUITY = "equity"


_STAGE_SLEEVE = {
    Stage.OVERWEIGHT_EQUITY: Sleeve.EQUITY,
    Stage.OVERWEIGHT_BONDS: Sleeve.BONDS,
    Stage.CASH: Sleeve.CASH,
    Stage.BONDS: Sleeve.BONDS,
    Stage.EQUITY: Sleeve.EQUITY,
}
_OVERWEIGHT = {Stage.OVERWEIGHT_EQUITY, Stage.OVERWEIGHT_BONDS}


class Inflation(StrEnum):
    INITIAL = "initial"
    APPLIED = "applied"
    FROZEN = "frozen"


class Guardrail(StrEnum):
    NONE = "none"
    CUT = "cut"
    RAISE = "raise"


@dataclass(frozen=True)
class Cell:
    # w0: the year-0 withdrawal over year-0 opening wealth.
    initial_rate: Fraction
    years: int

    def __post_init__(self) -> None:
        if self.initial_rate <= 0 or self.years <= 0:
            raise ValueError("a cell needs a positive initial rate and horizon")


@dataclass(frozen=True)
class Spending:
    inflated: Fraction
    inflation: Inflation
    guardrail: Guardrail
    withdrawal: Fraction


def apply_rules(
    cell: Cell, *, year: int, basis: Fraction, cpi_ratio: Fraction, lost: bool, opening_wealth: int
) -> Spending:
    """One review after year 0: `basis` is last year's withdrawal and `lost` a negative investment return."""
    reference = cell.initial_rate * opening_wealth
    inflated = basis * cpi_ratio
    frozen = lost and inflated > basis and inflated > reference
    amount = basis if frozen else inflated
    inflation = Inflation.FROZEN if frozen else Inflation.APPLIED
    if year < cell.years - PRESERVATION_OFF_FINAL_YEARS and amount > PRESERVATION_TRIGGER * reference:
        return Spending(inflated, inflation, Guardrail.CUT, amount * CUT)
    if amount < PROSPERITY_TRIGGER * reference:
        return Spending(inflated, inflation, Guardrail.RAISE, amount * RAISE)
    return Spending(inflated, inflation, Guardrail.NONE, amount)


@dataclass(frozen=True)
class YearRecord:
    """One review's intentions; paid amounts are the settled receipts', not these."""

    year: int
    opening_wealth: int
    # The settled wealth right after last year's withdrawal: this year's investment-return denominator.
    prior_book: int | None
    spending: Spending
    funding: tuple[tuple[Stage, int], ...]
    swept: int

    @property
    def requested(self) -> int:
        """The withdrawal in whole currency quanta, half up; the exact amount stays next year's basis."""
        return _round(self.spending.withdrawal)


@dataclass(frozen=True)
class _Review:
    cpi: int
    prices: dict[Sleeve, int]
    withdrawal: Fraction
    # Unknown until the month after the review settles.
    book: int | None


@dataclass
class Memory:
    """One path's state; a replay starts from a fresh instance."""

    records: list[YearRecord] = field(default_factory=list)
    last: _Review | None = None


def _round(amount: Fraction) -> int:
    return (2 * amount.numerator + amount.denominator) // (2 * amount.denominator)


class _Reservations:
    """Units each lot has left for this review's later stages, so no unit is sold twice."""

    def __init__(self, observation: Observation, year: int) -> None:
        self.observation = observation
        self.year = year
        self.lots = {
            sleeve: sorted(
                (
                    lot
                    for lot in observation.public_positions
                    if lot.account_id == BROKERAGE and lot.asset_id == AssetId(sleeve)
                ),
                key=lambda lot: (lot.purchase_month, lot.lot_id),
            )
            for sleeve in Sleeve
        }
        self.remaining = {lot.lot_id: lot.units for lots in self.lots.values() for lot in lots}

    def value(self, sleeve: Sleeve) -> int:
        return sum(lot.value for lot in self.lots[sleeve])

    def sell(self, sleeve: Sleeve, amount: int, label: str) -> tuple[list[Sell], int]:
        """FIFO over the unreserved units: at most one sale, and its quoted proceeds."""
        available: list[PublicPosition] = [
            lot.model_copy(update={"units": units, "value": quoted_value(units, lot.price, lot.quantity_scale)})
            for lot in self.lots[sleeve]
            if (units := self.remaining[lot.lot_id])
        ]
        lots, proceeds = sale_lots(available, amount)
        for lot in lots:
            self.remaining[lot.lot_id] -= lot.units
        sale = Sell(
            cause_id=f"gk-y{self.year}-{label}",
            agent_id=self.observation.agent_id,
            proceeds_account_id=CHECKING,
            asset_id=AssetId(sleeve),
            lots=tuple(lots),
        )
        return [sale] if lots else [], proceeds


def annual_actions(observation: Observation, cell: Cell, memory: Memory) -> list[Action]:
    """Review months emit funding sales, the withdrawal, then the sweep's sales and cash-sleeve purchase."""
    pools = {pool.asset_id: pool for pool in observation.holding_pools if pool.account_id == BROKERAGE}
    prices = {sleeve: pools[AssetId(sleeve)].price for sleeve in Sleeve}
    wealth = observation.cash + observation.public_holdings
    year, offset = divmod(observation.month, MONTHS_PER_YEAR)
    last = memory.last
    if offset or year == cell.years:
        if offset == 1:
            if last is None or last.book is not None:
                raise ValueError("the month after a review must follow exactly one review")
            if prices != last.prices:
                raise ValueError("the annual policy needs sleeve prices held between reviews")
            memory.last = replace(last, book=wealth)
        return []
    if observation.cpi is None:
        raise ValueError("inflation adjustment needs a supplied CPI path")
    if year != len(memory.records):
        raise ValueError(f"review {year=} after {len(memory.records)} earlier reviews")
    if last is None:
        withdrawal = cell.initial_rate * wealth
        spending = Spending(withdrawal, Inflation.INITIAL, Guardrail.NONE, withdrawal)
        rising: set[Sleeve] = set()
    else:
        if last.book is None:
            raise ValueError("no settled post-withdrawal book since the last review")
        spending = apply_rules(
            cell,
            year=year,
            basis=last.withdrawal,
            cpi_ratio=Fraction(observation.cpi[0], last.cpi),
            lost=wealth < last.book,
            opening_wealth=wealth,
        )
        rising = {sleeve for sleeve in (Sleeve.EQUITY, Sleeve.BONDS) if prices[sleeve] > last.prices[sleeve]}

    reservations = _Reservations(observation, year)
    excess = {
        sleeve: max(0, reservations.value(sleeve) - ceil(Fraction(ADAPTATION_TARGET_PERCENT[sleeve] * wealth, 100)))
        for sleeve in rising
    }
    requested = _round(spending.withdrawal)
    need = requested
    actions: list[Action] = []
    funding = []
    for stage in Stage:
        sleeve = _STAGE_SLEEVE[stage]
        if need <= 0 or (stage in _OVERWEIGHT and sleeve not in rising):
            continue
        raised = min(need, dict(observation.accounts)[CHECKING]) if stage is Stage.CASH else 0
        sales, proceeds = reservations.sell(
            sleeve, min(need, excess[sleeve]) if stage in _OVERWEIGHT else need - raised, stage
        )
        actions.extend(sales)
        if stage in _OVERWEIGHT:
            excess[sleeve] = max(0, excess[sleeve] - proceeds)
        if raised + proceeds:
            funding.append((stage, raised + proceeds))
        need -= raised + proceeds
    actions.append(
        Consume(
            request_id=0,
            cause_id=f"gk-y{year}-withdrawal",
            component_id="guyton_klinger_withdrawal",
            from_account=AccountRef(agent_id=observation.agent_id, account_id=CHECKING),
            to_account=AccountRef(agent_id=WORLD, account_id=CHECKING),
            amount=requested,
        )
    )
    swept = 0
    for sleeve in sorted(rising):
        sales, proceeds = reservations.sell(sleeve, excess[sleeve], f"sweep-{sleeve}")
        actions.extend(sales)
        swept += proceeds
    cash_pool = pools[AssetId(Sleeve.CASH)]
    if units := quantity_for_value(swept, cash_pool.price, cash_pool.quantity_scale, round_up=False):
        actions.append(
            Buy(
                cause_id=f"gk-y{year}-sweep-buy",
                agent_id=observation.agent_id,
                cash_account_id=CHECKING,
                holding_account_id=BROKERAGE,
                asset_id=cash_pool.asset_id,
                lot_id=LotId(f"gk-y{year}-sweep"),
                quantity_scale=cash_pool.quantity_scale,
                units=units,
            )
        )
    memory.records.append(
        YearRecord(year, wealth, None if last is None else last.book, spending, tuple(funding), swept)
    )
    memory.last = _Review(observation.cpi[0], prices, spending.withdrawal, None)
    return actions


class Policy:
    """The batch-policy callable; each path gets fresh memory on its first decision."""

    def __init__(self, cell: Cell) -> None:
        self.cell = cell
        self.memory: dict[int, Memory] = {}

    def __call__(self, batch: list[Decision]) -> list[DecisionActions]:
        return [
            DecisionActions(
                decision.rollout_id,
                decision.observation.month,
                annual_actions(decision.observation, self.cell, self.memory.setdefault(decision.rollout_id, Memory())),
            )
            for decision in batch
        ]
