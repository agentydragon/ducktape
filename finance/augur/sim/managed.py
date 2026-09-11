"""Settle the Python TLH component's cash and tax effects, never its private cohorts."""

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Literal

from finance.augur.sim.accounting import Accounting
from finance.augur.sim.actions import Contribute, Liquidate, Withdraw
from finance.augur.sim.books import AccountRef, DistributionOutcome, JournalEntry, Posting
from finance.augur.sim.fixed_point import MONEY_FACTOR_SCALE
from finance.augur.sim.holdings import gain_account
from finance.augur.sim.money import checked_count, mul_div
from finance.augur.sim.observations import TlhPortfolioObservation
from finance.augur.sim.prepared import PreparedScenario
from finance.augur.sim.scenario import InterestIncome

type Operation = Literal["modeled_realization", "contribution", "redemption", "distribution"]


@dataclass(frozen=True)
class InterestCredit:
    issuer_jurisdiction_id: str | None
    amount: int


@dataclass(frozen=True)
class ComponentEffects:
    observation: TlhPortfolioObservation
    cash_account_id: str | None
    cash_amount: int
    short_term_gain: int
    long_term_gain: int
    interest: tuple[InterestCredit, ...] = ()


@dataclass(frozen=True)
class FinancialEffect:
    month: int
    cause_id: str
    portfolio_id: str
    agent_id: str
    account_id: str
    cash_account_id: str | None
    operation: Operation
    cash_amount: int
    short_term_gain: int
    long_term_gain: int
    basis_change: int
    interest_income: int


def basis_account(observation: TlhPortfolioObservation) -> AccountRef:
    return AccountRef(
        agent_id=observation.owner_agent_id, account_id=f"asset:managed-portfolio:{observation.portfolio_id}"
    )


class ManagedPortfolios:
    def __init__(
        self, scenario: PreparedScenario, accounting: Accounting, observations: Sequence[TlhPortfolioObservation]
    ) -> None:
        self.marks: dict[str, TlhPortfolioObservation] = {}
        self.effects: list[FinancialEffect] = []
        self.effect_count = 0
        self.distributions: list[DistributionOutcome] = []
        self.distribution_count = 0
        if len({row.portfolio_id for row in observations}) != len(observations) or len(observations) != len(
            scenario.tlh_portfolios
        ):
            raise ValueError("opening component observations must cover exactly the declared portfolios")
        entries = []
        for row in observations:
            self.validate_observation(scenario, row)
            accounting.ledger.ensure_account(basis_account(row))
            accounting.ledger.ensure_account(gain_account(row.owner_agent_id))
            equity = AccountRef(agent_id=row.owner_agent_id, account_id="equity:opening")
            accounting.ledger.ensure_account(equity)
            entries.append(
                JournalEntry(
                    month=0,
                    cause_id=f"opening-component:{row.portfolio_id}",
                    postings=[
                        Posting(account=basis_account(row), amount=row.reported_tax_basis),
                        Posting(account=equity, amount=checked_count(-row.reported_tax_basis, "money negation")),
                    ],
                )
            )
        accounting.apply_entries(entries)
        self.marks = {row.portfolio_id: row for row in observations}

    @staticmethod
    def validate_observation(scenario: PreparedScenario, row: TlhPortfolioObservation) -> None:
        if (
            row.value < 0
            or row.reported_tax_basis < 0
            or not any(
                (spec.portfolio_id, spec.owner_agent_id, spec.account_id, spec.asset_id)
                == (row.portfolio_id, row.owner_agent_id, row.account_id, row.asset_id)
                for spec in scenario.tlh_portfolios
            )
        ):
            raise ValueError("component observation has unknown ownership or negative value/basis")
        checked_count(row.value, "component value")
        checked_count(row.reported_tax_basis, "component basis")

    def mark(self, scenario: PreparedScenario, observations: Sequence[TlhPortfolioObservation]) -> None:
        if len({row.portfolio_id for row in observations}) != len(observations) or len(observations) != len(self.marks):
            raise ValueError("component marks must cover exactly the existing portfolios")
        for row in observations:
            self.validate_observation(scenario, row)
            if (
                row.portfolio_id not in self.marks
                or row.reported_tax_basis != self.marks[row.portfolio_id].reported_tax_basis
            ):
                raise ValueError("a mark-only update cannot change component basis")
        self.marks = {row.portfolio_id: row for row in observations}

    def validate_request(self, action: Contribute | Withdraw | Liquidate, effects: ComponentEffects) -> None:
        row = effects.observation
        if isinstance(action, Contribute | Withdraw):
            if action.amount < 0:
                raise ValueError("component effects do not match the requested operation")
            expected = (
                checked_count(-action.amount, "money negation") if isinstance(action, Contribute) else action.amount
            )
        else:
            if action.portfolio_id not in self.marks or row.value != 0 or row.reported_tax_basis != 0:
                raise ValueError("component effects do not match the requested operation")
            expected = self.marks[action.portfolio_id].value
        if (row.owner_agent_id, row.portfolio_id, effects.cash_account_id, effects.cash_amount) != (
            action.agent_id,
            action.portfolio_id,
            action.cash_account_id,
            expected,
        ) or effects.interest:
            raise ValueError("component effects do not match the requested operation")

    def settle(
        self,
        scenario: PreparedScenario,
        accounting: Accounting,
        month: int,
        actor: str,
        cause: str,
        effects: ComponentEffects,
        *,
        operation: Operation,
        action: Contribute | Withdraw | Liquidate | None = None,
    ) -> None:
        if action is not None:
            if action.cause_id != cause:
                raise ValueError("action and component effect cause differ")
            self.validate_request(action, effects)
            operation = "contribution" if isinstance(action, Contribute) else "redemption"
        row = effects.observation
        self.validate_observation(scenario, row)
        for interest in effects.interest:
            source = InterestIncome(issuer_jurisdiction_id=interest.issuer_jurisdiction_id)
            if (
                interest.amount < 0
                or source not in scenario.income_sources
                or (
                    interest.issuer_jurisdiction_id is not None
                    and not any(
                        jurisdiction.jurisdiction_id == interest.issuer_jurisdiction_id
                        for jurisdiction in scenario.jurisdictions
                    )
                )
            ):
                raise ValueError("component interest needs a declared income source and nonnegative amount")
        if not cause or row.owner_agent_id != actor:
            raise ValueError("component effects need a cause and the component's owner")
        if row.portfolio_id not in self.marks:
            raise ValueError("component has no opening observation")
        basis_change = checked_count(
            row.reported_tax_basis - self.marks[row.portfolio_id].reported_tax_basis, "money subtraction"
        )
        capital_gain = checked_count(effects.short_term_gain + effects.long_term_gain, "money addition")
        interest_total = 0
        for interest in effects.interest:
            interest_total = checked_count(interest_total + interest.amount, "money addition")
        gain = checked_count(capital_gain + interest_total, "money addition")
        if checked_count(effects.cash_amount + basis_change, "money addition") != gain:
            raise ValueError("component cash, basis change and realized gains do not reconcile")
        postings = []
        if effects.cash_account_id is not None:
            cash = AccountRef(agent_id=actor, account_id=effects.cash_account_id)
            if cash not in accounting.declared:
                raise ValueError("unknown component cash account")
            if checked_count(accounting.ledger.balance(cash) + effects.cash_amount, "money addition") < 0:
                raise ValueError("component contribution exceeds available cash")
            postings.append(Posting(account=cash, amount=effects.cash_amount))
        elif effects.cash_amount != 0:
            raise ValueError("component cash movement needs a household account")
        postings.extend(
            [
                Posting(account=basis_account(row), amount=basis_change),
                Posting(account=gain_account(actor), amount=checked_count(-capital_gain, "money negation")),
            ]
        )
        if interest_total:
            postings.append(
                Posting(
                    account=AccountRef(agent_id="__external__", account_id="boundary"),
                    amount=checked_count(-interest_total, "money negation"),
                )
            )
        tax = deepcopy(accounting.tax)
        tax.gain(actor, effects.short_term_gain, long_term=False)
        tax.gain(actor, effects.long_term_gain, long_term=True)
        for interest in effects.interest:
            tax.income.accrue(
                actor, InterestIncome(issuer_jurisdiction_id=interest.issuer_jurisdiction_id), interest.amount
            )
        count = self.effect_count + 1
        if count >= 1 << 64:
            raise OverflowError("integer overflow during TLH financial effect count")
        accounting.apply(JournalEntry(month=month, cause_id=cause, postings=postings))
        accounting.tax = tax
        self.marks[row.portfolio_id] = row
        self.effect_count = count
        if accounting.capture != "summary":
            self.effects.append(
                FinancialEffect(
                    month,
                    cause,
                    row.portfolio_id,
                    actor,
                    row.account_id,
                    effects.cash_account_id,
                    operation,
                    effects.cash_amount,
                    effects.short_term_gain,
                    effects.long_term_gain,
                    basis_change,
                    interest_total,
                )
            )

    def distribute(
        self, scenario: PreparedScenario, accounting: Accounting, month: int, index: int, total: int
    ) -> None:
        if total < 0 or not 0 <= index < len(scenario.distributions):
            raise ValueError("negative or unknown component distribution")
        spec = scenario.distributions[index]
        observation = next(
            (
                row
                for row in self.marks.values()
                if (row.owner_agent_id, row.account_id, row.asset_id)
                == (spec.agent_id, spec.holding_account_id, spec.asset_id)
            ),
            None,
        )
        if observation is None:
            raise ValueError("distribution has no managed holding")
        outcomes, credits = [], []
        cash = 0
        for slice_index, slice_ in enumerate(spec.tax_character):
            amount = mul_div(total, slice_.fraction_ppb, MONEY_FACTOR_SCALE, "security distribution tax slice")
            cash = checked_count(cash + amount, "money addition")
            credits.append(InterestCredit(slice_.issuer_jurisdiction_id, amount))
            outcomes.append(
                DistributionOutcome(
                    month=month,
                    agent_id=spec.agent_id,
                    holding_account_id=spec.holding_account_id,
                    asset_id=spec.asset_id,
                    slice_index=slice_index,
                    fraction_ppb=slice_.fraction_ppb,
                    issuer_jurisdiction_id=slice_.issuer_jurisdiction_id,
                    units=None,
                    amount=amount,
                )
            )
        count = self.distribution_count + len(outcomes)
        if count >= 1 << 64:
            raise OverflowError("integer overflow during distribution count")
        self.settle(
            scenario,
            accounting,
            month,
            spec.agent_id,
            f"distribution:{spec.agent_id}:{spec.asset_id}:m{month}",
            ComponentEffects(observation, spec.to_account_id, cash, 0, 0, tuple(credits)),
            operation="distribution",
        )
        self.distribution_count = count
        if accounting.capture != "summary":
            self.distributions.extend(outcomes)
