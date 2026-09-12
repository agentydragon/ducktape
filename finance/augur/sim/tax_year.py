"""One taxpayer's annual facts, with jurisdiction-specific assessment at year close."""

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass

from finance.augur.sim.books import TaxAccrual
from finance.augur.sim.compiler.tax import PreparedTaxProfile
from finance.augur.sim.fixed_point import MONEY_FACTOR_SCALE
from finance.augur.sim.money import MAX_COUNT, checked_count, checked_wide, mul_div, round_ratio
from finance.augur.sim.mortgage import Mortgage
from finance.augur.sim.prepared import PreparedScenario
from finance.augur.sim.scenario import InterestIncome, OrdinaryIncome, TransferIncomeCategory
from finance.augur.sim.tax import IncomeLedger, TaxFacts, assess, net_capital_gains, taxes_interest_from


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
    def __init__(self, profiles: Sequence[PreparedTaxProfile], sources: Sequence[TransferIncomeCategory]) -> None:
        self.years = {profile.agent_id: TaxYear() for profile in profiles}
        self.income = IncomeLedger(self.years, sources)

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

    def assessments(self, scenario: PreparedScenario, month: int, mortgages: Sequence[Mortgage]) -> list[TaxAccrual]:
        """Quote the whole close without mutating income, carryovers, or financial balances."""
        income = deepcopy(self.income)
        rows: list[TaxAccrual] = []
        levels = {jurisdiction.jurisdiction_id: jurisdiction.level for jurisdiction in scenario.jurisdictions}
        for profile in scenario.tax_profiles:
            year = self.years[profile.agent_id]
            gains = net_capital_gains(
                year.short_term_gain,
                year.long_term_gain,
                year.capital_loss_carryforward,
                profile.jurisdictions[0].max_capital_loss_ordinary_offset,
            )
            for deduction in (year.depreciation_deduction, year.rental_interest_deduction, gains.ordinary_offset):
                income.deduct_from_ordinary(profile.agent_id, deduction)
            annual = []
            for rules in profile.jurisdictions:
                taxable = 0
                for (agent, source), amount in income.by_source.items():
                    if agent != profile.agent_id:
                        continue
                    if isinstance(source, OrdinaryIncome) or (
                        isinstance(source, InterestIncome)
                        and taxes_interest_from(
                            rules,
                            source.issuer_jurisdiction_id,
                            levels.get(source.issuer_jurisdiction_id) if source.issuer_jurisdiction_id else None,
                        )
                    ):
                        taxable = checked_count(taxable + amount, "money addition")
                mortgage_deduction = mortgage_interest_deduction(
                    scenario, mortgages, profile.agent_id, rules.jurisdiction_id
                )
                facts = TaxFacts(
                    taxable_ordinary_income=taxable,
                    short_term_gain=gains.short_term,
                    long_term_gain=gains.long_term,
                    section_1250_recapture=year.section_1250_recapture,
                    itemized_deduction=mortgage_deduction,
                    mortgage_interest_deduction=mortgage_deduction,
                    rental_interest_deduction=year.rental_interest_deduction,
                    depreciation_deduction=year.depreciation_deduction,
                    property_tax_paid=year.property_tax_paid,
                )
                annual.append((rules, facts, assess(facts, rules)))
            policy = next(
                (
                    policy
                    for policy in scenario._federal_salt_deduction_policies
                    if policy.profile_id == profile.agent_id
                ),
                None,
            )
            if policy is not None:
                state_tax = 0
                for rules, _, assessment in annual:
                    if rules.jurisdiction_id != policy.federal_jurisdiction_id:
                        state_tax = checked_count(state_tax + assessment.total_tax, "money addition")
                caps = [cap for cap in policy.cap_schedule if cap.effective_year_index <= month // 12]
                cap = (
                    max(caps, key=lambda cap: cap.effective_year_index).cap
                    if caps
                    else (0 if policy.cap_schedule else MAX_COUNT)
                )
                for index, (rules, facts, _) in enumerate(annual):
                    if rules.jurisdiction_id == policy.federal_jurisdiction_id:
                        facts.salt_deduction = min(
                            cap, checked_count(facts.property_tax_paid + state_tax, "money addition")
                        )
                        facts.itemized_deduction = checked_count(
                            facts.mortgage_interest_deduction + facts.salt_deduction, "money addition"
                        )
                        annual[index] = (rules, facts, assess(facts, rules))
            for rules, facts, assessment in annual:
                rows.append(
                    TaxAccrual(
                        month=month,
                        cause_id=f"{profile.agent_id}_{rules.jurisdiction_id}_year_end_accrual_m{month}",
                        agent_id=profile.agent_id,
                        jurisdiction_id=rules.jurisdiction_id,
                        tax_year_end_month=month,
                        ordinary_income=checked_count(
                            income.ordinary(profile.agent_id) - assessment.ordinary_loss_offset, "money subtraction"
                        ),
                        short_term_gain=assessment.short_term_gain,
                        long_term_gain=assessment.long_term_gain,
                        section_1250_recapture=facts.section_1250_recapture,
                        rental_interest_deduction=facts.rental_interest_deduction,
                        depreciation_deduction=facts.depreciation_deduction,
                        standard_deduction=rules.standard_deduction,
                        mortgage_interest_deduction=facts.mortgage_interest_deduction,
                        salt_deduction=facts.salt_deduction,
                        itemized_deduction=facts.itemized_deduction,
                        ordinary_taxable=assessment.ordinary_taxable,
                        long_term_capital_gain_taxable=assessment.long_term_capital_gain_taxable,
                        ordinary_tax=assessment.ordinary_tax,
                        capital_gain_tax=assessment.capital_gain_tax,
                        section_1250_tax=assessment.section_1250_tax,
                        total_tax=assessment.total_tax,
                        capital_loss_carryforward=gains.carryforward,
                    )
                )
        return rows

    def reset(self, assessments: Sequence[TaxAccrual]) -> None:
        for assessment in assessments:
            self.years[assessment.agent_id] = TaxYear(capital_loss_carryforward=assessment.capital_loss_carryforward)
            self.income.reset(assessment.agent_id)


def mortgage_interest_deduction(
    scenario: PreparedScenario, mortgages: Sequence[Mortgage], agent: str, jurisdiction: str
) -> int:
    numerator = 0
    by_id = {mortgage.terms.liability_id: mortgage for mortgage in mortgages}
    for policy in scenario._mortgage_interest_deduction_policies:
        if policy.owner_agent_id != agent or policy.liability_id not in by_id:
            continue
        mortgage = by_id[policy.liability_id]
        principal = mortgage.terms.origination_principal
        cap = (
            policy.per_jurisdiction_principal_cap.get(jurisdiction, 0)
            if policy.per_jurisdiction_principal_cap
            else principal
        )
        factor = (
            0
            if policy.debt_class == "home_equity"
            else mul_div(min(cap, principal), MONEY_FACTOR_SCALE, principal, "mortgage-interest principal factor")
        )
        owner_interest = checked_count(
            mortgage.interest_paid_ytd - mortgage.rental_interest_paid_ytd, "money subtraction"
        )
        numerator = checked_wide(
            numerator + checked_wide(owner_interest * factor, "mortgage-interest scaled deduction"),
            "mortgage-interest aggregate deduction",
        )
    return checked_count(round_ratio(numerator, MONEY_FACTOR_SCALE), "mortgage-interest aggregate deduction")
