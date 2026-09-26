"""The authority a taxpayer's profile names: estimated instalments, the year's assessment at its close, a true-up."""

from collections.abc import Sequence

from finance.augur.sim.accounting import Accounting, TaxLiabilityStatement
from finance.augur.sim.actor import Actor, MonthOpened
from finance.augur.sim.books import AccountRef, JournalEntry, Posting, TaxAccrual, TaxLiabilityState
from finance.augur.sim.claims import Demand, TaxPayment, TaxTrueUp
from finance.augur.sim.compiler.tax import PreparedTaxProfile
from finance.augur.sim.fixed_point import MONEY_FACTOR_SCALE
from finance.augur.sim.ids import AccountId, JurisdictionId
from finance.augur.sim.money import MAX_COUNT, checked_count, checked_wide, mul_div, round_ratio
from finance.augur.sim.mortgage import Mortgage
from finance.augur.sim.prepared import PreparedJurisdiction, _MortgageInterestDeduction, _SaltDeduction
from finance.augur.sim.scenario import InterestIncome, OrdinaryIncome
from finance.augur.sim.tax import TaxFacts, assess, net_capital_gains, taxes_interest_from
from finance.augur.sim.tax_year import TaxBook


class Assessment(Demand):
    amount: int
    effect: TaxPayment | TaxTrueUp


class TaxAuthority(Actor[MonthOpened | TaxLiabilityStatement, Assessment]):
    """One taxpayer's tax: closes each year from the facts settlement recorded and posts the assessment.

    Estimates through the year are a quarter of the profile's prior-year tax each; January's
    fourth estimate and true-up settle the liability the close posted, which the authority
    reads back from its `TaxLiabilityStatement`.
    """

    def __init__(self, profile: PreparedTaxProfile) -> None:
        self.profile = profile
        self.liabilities: TaxLiabilityStatement | None = None
        self.salt_policies: tuple[_SaltDeduction, ...] = ()
        self.mortgage_interest_policies: tuple[_MortgageInterestDeduction, ...] = ()

    def declare_deduction(self, policy: _MortgageInterestDeduction | _SaltDeduction) -> None:
        """An itemized deduction this authority's taxpayer claims when its tax year closes."""
        claimant = policy.owner_agent_id if isinstance(policy, _MortgageInterestDeduction) else policy.profile_id
        if claimant != self.profile.agent_id:
            raise ValueError(f"a deduction claimed by {claimant!r} is not {self.profile.agent_id!r}'s to assess")
        if isinstance(policy, _MortgageInterestDeduction):
            self.mortgage_interest_policies = (*self.mortgage_interest_policies, policy)
        else:
            self.salt_policies = (*self.salt_policies, policy)

    def handle(self, message: MonthOpened | TaxLiabilityStatement) -> list[Assessment]:
        if isinstance(message, TaxLiabilityStatement):
            self.liabilities = message
            return []
        profile, month = self.profile, message.month
        quarter = {3: 1, 5: 2, 8: 3}.get(month % 12)
        if quarter is None and not (month > 0 and month % 12 == 0):
            return []
        source = AccountRef(agent_id=profile.agent_id, account_id=profile.payment_account_id)
        destination = AccountRef(agent_id=profile.tax_authority_agent_id, account_id=profile.tax_authority_account_id)
        if quarter is not None:
            if profile.prior_year_tax <= 0:
                return []
            amount = mul_div(profile.prior_year_tax, 1, 4, "quarterly estimated tax")
            if not amount:
                return []
            return [
                Assessment(
                    cause_id=f"{profile.agent_id}_estimated_tax_q{quarter}_y{month // 12}",
                    obligation_type="estimated_tax",
                    from_account=source,
                    to_account=destination,
                    amount=amount,
                    effect=TaxPayment(profile),
                )
            ]
        if self.liabilities is None or self.liabilities.month != month:
            raise ValueError("a year-end assessment needs this month's liability statement")
        tax_year = month // 12 - 1
        year_end = tax_year * 12 + 11
        actual = 0
        for liability in self.liabilities.liabilities:
            if liability.active and liability.agent_id == profile.agent_id and liability.tax_year_end_month == year_end:
                actual = checked_count(actual + liability.amount_owed, "money addition")
        safe_harbor = min(profile.prior_year_tax, actual)
        previous_quarters = mul_div(profile.prior_year_tax, 3, 4, "first three estimated-tax quarters")
        assessments = []
        q4 = max(0, safe_harbor - previous_quarters)
        if q4:
            assessments.append(
                Assessment(
                    cause_id=f"{profile.agent_id}_estimated_tax_q4_y{tax_year}",
                    obligation_type="estimated_tax",
                    from_account=source,
                    to_account=destination,
                    amount=q4,
                    effect=TaxPayment(profile),
                )
            )
        true_up = max(0, actual - safe_harbor)
        if true_up:
            assessments.append(
                Assessment(
                    cause_id=f"{profile.agent_id}_tax_true_up_y{tax_year}",
                    obligation_type="tax_true_up",
                    from_account=source,
                    to_account=destination,
                    amount=true_up,
                    effect=TaxTrueUp(profile, year_end),
                )
            )
        return assessments

    def assessments(
        self, book: TaxBook, month: int, mortgages: Sequence[Mortgage], jurisdictions: Sequence[PreparedJurisdiction]
    ) -> list[TaxAccrual]:
        """Quote the year's close, one row per profile jurisdiction, without mutating the book.

        `jurisdictions` is the world's vocabulary, which says whose interest a rule exempts.
        """
        profile = self.profile
        agent = profile.agent_id
        income = book.income.copy()
        year = book.years[agent]
        levels = {jurisdiction.jurisdiction_id: jurisdiction.level for jurisdiction in jurisdictions}
        gains = net_capital_gains(
            year.short_term_gain,
            year.long_term_gain,
            year.capital_loss_carryforward,
            profile.jurisdictions[0].max_capital_loss_ordinary_offset,
        )
        for deduction in (year.depreciation_deduction, year.rental_interest_deduction, gains.ordinary_offset):
            income.deduct_from_ordinary(agent, deduction)
        annual = []
        for rules in profile.jurisdictions:
            taxable = 0
            for (owner, source), amount in income.by_source.items():
                if owner != agent:
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
                self.mortgage_interest_policies, mortgages, rules.jurisdiction_id
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
        # The first declared SALT policy is the one claimed.
        if self.salt_policies:
            policy = self.salt_policies[0]
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
        return [
            TaxAccrual(
                month=month,
                cause_id=f"{agent}_{rules.jurisdiction_id}_year_end_accrual_m{month}",
                agent_id=agent,
                jurisdiction_id=rules.jurisdiction_id,
                tax_year_end_month=month,
                ordinary_income=checked_count(
                    income.ordinary(agent) - assessment.ordinary_loss_offset, "money subtraction"
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
            for rules, facts, assessment in annual
        ]

    def close_month(
        self,
        accounting: Accounting,
        month: int,
        mortgages: Sequence[Mortgage],
        jurisdictions: Sequence[PreparedJurisdiction],
    ) -> None:
        """At the tax year's last month, post the assessment as expense against liability and reset the year.

        `mortgages` are the contracts whose interest paid this year the deductions read.
        """
        if month % 12 != 11:
            return
        rows = self.assessments(accounting.tax, month, mortgages, jurisdictions)
        # One group, so a bad jurisdiction does not commit the jurisdictions assessed before it.
        accounting.apply_entries(
            [
                JournalEntry(
                    month=month,
                    cause_id=row.cause_id,
                    postings=[
                        Posting(
                            account=AccountRef(
                                agent_id=row.agent_id, account_id=AccountId(f"expense:tax:{row.jurisdiction_id}")
                            ),
                            amount=row.total_tax,
                        ),
                        Posting(
                            account=AccountRef(
                                agent_id=row.agent_id, account_id=AccountId(f"liability:tax:{row.jurisdiction_id}")
                            ),
                            amount=checked_count(-row.total_tax, "money negation"),
                        ),
                    ],
                )
                for row in rows
                if row.total_tax
            ]
        )
        accounting.tax_accruals.extend(rows)
        accounting.tax_liabilities.extend(
            TaxLiabilityState(
                agent_id=row.agent_id,
                jurisdiction_id=row.jurisdiction_id,
                tax_year_end_month=month,
                amount_owed=row.total_tax,
                active=True,
            )
            for row in rows
        )
        accounting.tax.reset(rows)


def mortgage_interest_deduction(
    policies: Sequence[_MortgageInterestDeduction], mortgages: Sequence[Mortgage], jurisdiction: JurisdictionId
) -> int:
    """The owner-occupied share of the interest paid this year on each claimed loan, capped per jurisdiction."""
    numerator = 0
    by_id = {mortgage.terms.liability_id: mortgage for mortgage in mortgages}
    for policy in policies:
        if policy.liability_id not in by_id:
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
