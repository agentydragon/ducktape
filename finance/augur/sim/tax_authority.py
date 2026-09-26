"""The authority a taxpayer's profile names: estimated instalments through the year, a true-up after it."""

from finance.augur.sim.accounting import TaxLiabilityStatement
from finance.augur.sim.actor import Actor, MonthOpened
from finance.augur.sim.books import AccountRef
from finance.augur.sim.claims import Demand, TaxPayment, TaxTrueUp
from finance.augur.sim.compiler.tax import PreparedTaxProfile
from finance.augur.sim.money import checked_count, mul_div


class Assessment(Demand):
    amount: int
    effect: TaxPayment | TaxTrueUp


class TaxAuthority(Actor[MonthOpened | TaxLiabilityStatement, Assessment]):
    """Thin first form: assesses from what the tax book already computed; the year close stays in `Accounting`."""

    def __init__(self, profile: PreparedTaxProfile) -> None:
        self.profile = profile
        self.liabilities: TaxLiabilityStatement | None = None

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
