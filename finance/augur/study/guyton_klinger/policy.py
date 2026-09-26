"""Guyton-Klinger 2006 four-rule annual policy over one cash, one bond and one equity sleeve.

Runs on the declared adaptation (<README.md>) as <paths.py> composes it: `ADAPTATION_TARGET_PERCENT`,
each sleeve's pool in `BROKERAGE` named by its `Sleeve` value, `CHECKING` only settlement cash, each
sleeve's payouts in its `INCOME` account, tax claims paid from `TAX_RESERVE`, and spending consumed
into `WORLD`'s checking. Opening wealth is everything the retiree holds except the tax reserve.
Reviews fall at months 0, 12, …; the month after a review records the settled post-withdrawal
wealth as the next investment-return denominator; other months only pay tax claims that fall due.

Readings of the source contract's open decisions, recorded with reasons in that doc:

- ORDER: the prior withdrawal is scaled by the preceding year's CPI ratio, deflation included; the
  freeze withholds only an increase; then at most one guardrail is tested on that candidate against
  opening wealth. The adjusted amount is next year's basis, not inflated again this year.
- PORTFOLIO: a sleeve is its lots plus its unspent payouts. It is overweight by its value above its
  target share of opening wealth, before this review's trades, and only if its total return over
  the preceding year was positive. Funding follows `Stage` declaration order, drawing a sleeve's
  payouts before selling its lots; the sweep sells what funding left of that excess into the cash
  sleeve, and payouts nothing drew reinvest in the sleeve that paid them.
- OPENING: targets cover all opening wealth, with no separate first-withdrawal reserve; year 0 knows
  no returns, so nothing is overweight. Capital preservation applies while the zero-based year index
  is below `years - PRESERVATION_OFF_FINAL_YEARS`.
- TAXES, prior-year reserve: the withdrawal W is gross. Each review withholds from W what repays
  earlier tax advances, then moves the last closed year's assessed tax (none before the first
  close) into the tax reserve, and spends the rest. A due tax claim is paid from the reserve; what
  the reserve cannot cover is funded through the `Stage` order (between reviews, without the
  overweight stages) as a tax advance. At the review after a tax year closes and its claims are
  paid, the reserve's remainder is spent. A year's spending, W less its reserve plus that remainder
  less the advance its tax needed, is thus W less its tax, and the portfolio's outflow net of
  repayments is W.
"""

from dataclasses import dataclass, field, replace
from enum import StrEnum
from fractions import Fraction
from math import ceil

from finance.augur.policy.funding import full_payments
from finance.augur.policy.sleeves import quoted_value, sale_lots
from finance.augur.sim.actions import Action, Buy, Consume, DecisionActions, Sell, Transfer
from finance.augur.sim.books import AccountRef
from finance.augur.sim.claims import AssessmentDue
from finance.augur.sim.fixed_point import quantity_for_value
from finance.augur.sim.ids import AccountId, AssetId, LotId
from finance.augur.sim.observations import Decision, Observation, PublicPosition
from finance.augur.study.guyton_klinger.panel import PRICED, Sleeve
from finance.augur.study.guyton_klinger.paths import (
    ADAPTATION_TARGET_PERCENT,
    BROKERAGE,
    CHECKING,
    INCOME,
    MONTHS_PER_YEAR,
    TAX_RESERVE,
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
    # Unreserved checking first, then the cash sleeve's payouts and units.
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
    # Withheld from the withdrawal to repay earlier tax advances; it stays in the portfolio.
    repaid: int
    # Moved from the withdrawal into the tax reserve toward this year's tax.
    reserved: int
    # The last tax year's reserve left once its claims were paid, spent at this review.
    settled: int

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
    # Tax the portfolio advanced since the review: outflow the next investment return adds back.
    advanced: int


@dataclass
class Memory:
    """One path's state; a replay starts from a fresh instance."""

    records: list[YearRecord] = field(default_factory=list)
    last: _Review | None = None
    # Tax advanced from the portfolio that no withdrawal has repaid yet.
    debt: int = 0


def _round(amount: Fraction) -> int:
    return (2 * amount.numerator + amount.denominator) // (2 * amount.denominator)


class _Reservations:
    """Payouts and units each sleeve has left for this decision's later stages, so none is used twice."""

    def __init__(self, observation: Observation, prefix: str) -> None:
        self.observation = observation
        self.prefix = prefix
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
        accounts = dict(observation.accounts)
        self.income = {sleeve: accounts.get(INCOME[sleeve], 0) for sleeve in Sleeve}

    def value(self, sleeve: Sleeve) -> int:
        return sum(lot.value for lot in self.lots[sleeve]) + self.income[sleeve]

    def transfer(self, source: AccountId, destination: AccountId, amount: int, label: str) -> Transfer:
        agent = self.observation.agent_id
        return Transfer(
            cause_id=f"{self.prefix}-{label}",
            from_account=AccountRef(agent_id=agent, account_id=source),
            to_account=AccountRef(agent_id=agent, account_id=destination),
            amount=amount,
        )

    def draw(self, sleeve: Sleeve, amount: int, label: str) -> tuple[list[Action], int]:
        """The sleeve's unspent payouts into checking, then at most one FIFO sale over its unreserved units."""
        taken = min(amount, self.income[sleeve])
        self.income[sleeve] -= taken
        available: list[PublicPosition] = [
            lot.model_copy(update={"units": units, "value": quoted_value(units, lot.price, lot.quantity_scale)})
            for lot in self.lots[sleeve]
            if (units := self.remaining[lot.lot_id])
        ]
        lots, proceeds = sale_lots(available, amount - taken)
        for lot in lots:
            self.remaining[lot.lot_id] -= lot.units
        actions: list[Action] = []
        if taken:
            actions.append(self.transfer(INCOME[sleeve], CHECKING, taken, f"{label}-payouts"))
        if lots:
            actions.append(
                Sell(
                    cause_id=f"{self.prefix}-{label}",
                    agent_id=self.observation.agent_id,
                    proceeds_account_id=CHECKING,
                    asset_id=AssetId(sleeve),
                    lots=tuple(lots),
                )
            )
        return actions, taken + proceeds


def _fund(
    reservations: _Reservations, need: int, excess: dict[Sleeve, int]
) -> tuple[list[Action], list[tuple[Stage, int]]]:
    """Raise `need` into checking in `Stage` order; an overweight stage runs only for a sleeve in `excess`."""
    checking = dict(reservations.observation.accounts)[CHECKING]
    actions: list[Action] = []
    funding = []
    for stage in Stage:
        sleeve = _STAGE_SLEEVE[stage]
        if need <= 0 or (stage in _OVERWEIGHT and sleeve not in excess):
            continue
        raised = min(need, checking) if stage is Stage.CASH else 0
        sales, proceeds = reservations.draw(
            sleeve, min(need, excess[sleeve]) if stage in _OVERWEIGHT else need - raised, stage
        )
        actions.extend(sales)
        if stage in _OVERWEIGHT:
            excess[sleeve] = max(0, excess[sleeve] - proceeds)
        if raised + proceeds:
            funding.append((stage, raised + proceeds))
        need -= raised + proceeds
    return actions, funding


def _tax_claims(observation: Observation) -> tuple[AssessmentDue, ...]:
    for claim in observation.claims:
        if not isinstance(claim, AssessmentDue):
            raise ValueError(f"the annual policy pays only tax assessments, not {claim.cause_id!r}")
    return tuple(claim for claim in observation.claims if isinstance(claim, AssessmentDue))


def _pay_between_reviews(observation: Observation, memory: Memory) -> list[Action]:
    """Pay the month's tax claims from the reserve, advancing what it lacks from the portfolio."""
    claims = _tax_claims(observation)
    if not claims:
        return []
    if memory.last is None or memory.last.book is None:
        raise ValueError("tax claims between reviews need a settled review before them")
    reservations = _Reservations(observation, f"gk-m{observation.month}-tax")
    advance = max(0, sum(claim.amount_due for claim in claims) - dict(observation.accounts)[TAX_RESERVE])
    actions, _ = _fund(reservations, advance, {})
    if advance:
        actions.append(reservations.transfer(CHECKING, TAX_RESERVE, advance, "advance"))
    memory.debt += advance
    memory.last = replace(memory.last, advanced=memory.last.advanced + advance)
    return [*actions, *full_payments(claims)]


def annual_actions(observation: Observation, cell: Cell, memory: Memory) -> list[Action]:
    """Review months emit funding, tax and spending, then the sweep and reinvestment; other months pay due tax."""
    accounts = dict(observation.accounts)
    pools = {pool.asset_id: pool for pool in observation.holding_pools if pool.account_id == BROKERAGE}
    prices = {sleeve: pools[AssetId(sleeve)].price for sleeve in Sleeve}
    reserve = accounts.get(TAX_RESERVE, 0)
    wealth = observation.cash + observation.public_holdings - reserve
    year, offset = divmod(observation.month, MONTHS_PER_YEAR)
    last = memory.last
    if offset:
        if offset == 1:
            if last is None or last.book is not None:
                raise ValueError("the month after a review must follow exactly one review")
            if prices != last.prices:
                raise ValueError("the annual policy needs sleeve prices held between reviews")
            memory.last = replace(last, book=wealth)
        return _pay_between_reviews(observation, memory)
    if observation.cpi is None:
        raise ValueError("inflation adjustment needs a supplied CPI path")
    if year != len(memory.records):
        raise ValueError(f"review {year=} after {len(memory.records)} earlier reviews")
    reservations = _Reservations(observation, f"gk-y{year}")
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
            lost=wealth + last.advanced < last.book,
            opening_wealth=wealth,
        )
        # A positive total return: the units' price change plus the payouts they earned, in quanta.
        rising = {
            sleeve
            for sleeve in PRICED
            if (prices[sleeve] - last.prices[sleeve]) * sum(lot.units for lot in reservations.lots[sleeve])
            + reservations.income[sleeve] * pools[AssetId(sleeve)].quantity_scale
            > 0
        }
    excess = {
        sleeve: max(0, reservations.value(sleeve) - ceil(Fraction(ADAPTATION_TARGET_PERCENT[sleeve] * wealth, 100)))
        for sleeve in rising
    }
    claims = _tax_claims(observation)
    due = sum(claim.amount_due for claim in claims)
    requested = _round(spending.withdrawal)
    shortfall = max(0, due - reserve)
    repaid = min(memory.debt + shortfall, requested)
    memory.debt += shortfall - repaid
    liabilities = () if observation.tax_records is None else observation.tax_records.liabilities
    last_year_tax = sum(row.amount_owed for row in liabilities if row.tax_year_end_month == observation.month - 1)
    # TODO: a reserve estimating this year's tax; last year's leaves a rising tax short every
    #   year, and the next withdrawal repays the advance a year late, interest-free.
    reserved = min(last_year_tax, requested - repaid)
    actions, funding = _fund(reservations, shortfall + requested - repaid, excess)
    if shortfall:
        actions.append(reservations.transfer(CHECKING, TAX_RESERVE, shortfall, "tax-shortfall"))
    actions.extend(full_payments(claims))
    settled = reserve + shortfall - due
    destination = AccountRef(agent_id=WORLD, account_id=CHECKING)
    if settled:
        actions.append(
            Consume(
                request_id=len(claims) + 1,
                cause_id=f"gk-y{year - 1}-settlement",
                component_id="guyton_klinger_withdrawal",
                from_account=AccountRef(agent_id=observation.agent_id, account_id=TAX_RESERVE),
                to_account=destination,
                amount=settled,
            )
        )
    if reserved:
        actions.append(reservations.transfer(CHECKING, TAX_RESERVE, reserved, "tax-reserve"))
    if spent := requested - repaid - reserved:
        actions.append(
            Consume(
                request_id=0,
                cause_id=f"gk-y{year}-withdrawal",
                component_id="guyton_klinger_withdrawal",
                from_account=AccountRef(agent_id=observation.agent_id, account_id=CHECKING),
                to_account=destination,
                amount=spent,
            )
        )
    swept = 0
    for sleeve in sorted(rising):
        sales, proceeds = reservations.draw(sleeve, excess[sleeve], f"sweep-{sleeve}")
        actions.extend(sales)
        swept += proceeds
    for sleeve in Sleeve:
        # TODO: wash sales: funding may sell a sleeve's lots at a loss that this month's
        #   reinvestment in the same sleeve would disallow. And a lot bought at one review and sold
        #   at the next counts long-term, as the engine counts 12 months; statute needs more than a year.
        payouts = reservations.income[sleeve]
        if payouts:
            actions.append(reservations.transfer(INCOME[sleeve], CHECKING, payouts, f"reinvest-{sleeve}"))
        pool = pools[AssetId(sleeve)]
        budget = payouts + (swept if sleeve is Sleeve.CASH else 0)
        if units := quantity_for_value(budget, pool.price, pool.quantity_scale, round_up=False):
            actions.append(
                Buy(
                    cause_id=f"gk-y{year}-buy-{sleeve}",
                    agent_id=observation.agent_id,
                    cash_account_id=CHECKING,
                    holding_account_id=BROKERAGE,
                    asset_id=pool.asset_id,
                    lot_id=LotId(f"gk-y{year}-{sleeve}"),
                    quantity_scale=pool.quantity_scale,
                    units=units,
                )
            )
    memory.records.append(
        YearRecord(
            year,
            wealth,
            None if last is None else last.book,
            spending,
            tuple(funding),
            swept,
            repaid,
            reserved,
            settled,
        )
    )
    memory.last = _Review(observation.cpi[0], prices, spending.withdrawal, None, 0)
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
