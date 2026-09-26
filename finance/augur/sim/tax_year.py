"""Each taxpayer's facts for the open tax year, which settlement records and the year close reads."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from finance.augur.sim.books import TaxAccrual
from finance.augur.sim.fixed_point import MONEY_FACTOR_SCALE
from finance.augur.sim.money import checked_count, mul_div
from finance.augur.sim.scenario import TransferIncomeCategory
from finance.augur.sim.tax import IncomeLedger


@dataclass
class TaxYear:
    short_term_gain: int = 0
    long_term_gain: int = 0
    section_1250_recapture: int = 0
    capital_loss_carryforward: int = 0
    rental_interest_deduction: int = 0
    depreciation_deduction: int = 0
    property_tax_paid: int = 0


class TaxBook:
    """Each enrolled taxpayer's facts for the open tax year, recorded as settlement posts them.

    A settlement path writes a copy and swaps it in with its journal entries, so the facts
    commit with the money they describe. The taxpayer's `TaxAuthority` reads them to close
    the year and resets them when it posts the assessment.
    """

    def __init__(self, sources: Sequence[TransferIncomeCategory]) -> None:
        self.years: dict[str, TaxYear] = {}
        self.income = IncomeLedger(sources)

    def enroll(self, agent_id: str) -> None:
        if agent_id in self.years:
            raise ValueError(f"taxpayer {agent_id!r} is already enrolled")
        self.years[agent_id] = TaxYear()
        self.income.enroll(agent_id)

    def copy(self) -> TaxBook:
        """Years and income rows hold only ints: fresh containers detach the copy from the book."""
        clone = TaxBook(self.income.sources)
        clone.years = {agent: replace(year) for agent, year in self.years.items()}
        clone.income = self.income.copy()
        return clone

    def gain(self, agent: str, amount: int, *, long_term: bool) -> None:
        if agent not in self.years:
            return
        year = self.years[agent]
        if long_term:
            year.long_term_gain = checked_count(year.long_term_gain + amount, "money addition")
        else:
            year.short_term_gain = checked_count(year.short_term_gain + amount, "money addition")

    def property_tax(self, agent: str, amount: int, rented_fraction: int) -> None:
        rental = mul_div(amount, rented_fraction, MONEY_FACTOR_SCALE, "rental property tax deduction")
        owner = mul_div(amount, MONEY_FACTOR_SCALE - rented_fraction, MONEY_FACTOR_SCALE, "owner property tax")
        self.income.deduct_from_ordinary(agent, rental)
        if agent in self.years:
            year = self.years[agent]
            year.property_tax_paid = checked_count(year.property_tax_paid + owner, "money addition")

    def reset(self, assessments: Sequence[TaxAccrual]) -> None:
        for assessment in assessments:
            self.years[assessment.agent_id] = TaxYear(capital_loss_carryforward=assessment.capital_loss_carryforward)
            self.income.reset(assessment.agent_id)
