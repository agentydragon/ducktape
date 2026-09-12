"""Current-month contractual demands; assembly neither funds nor settles an occurrence."""

from collections.abc import Sequence
from dataclasses import dataclass

from finance.augur.sim.actions import ClaimId
from finance.augur.sim.books import AccountRef, PropertyState, TaxLiabilityState
from finance.augur.sim.compiler.tax import PreparedTaxProfile
from finance.augur.sim.fixed_point import MONEY_FACTOR_SCALE
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.money import checked_count, checked_wide, mul_div, mul_div_wide
from finance.augur.sim.mortgage import MortgagePayment
from finance.augur.sim.prepared import PreparedScenario


@dataclass(frozen=True)
class OrdinaryDeduction:
    fraction: int


@dataclass(frozen=True)
class TaxPayment:
    profile: PreparedTaxProfile


@dataclass(frozen=True)
class TaxTrueUp:
    profile: PreparedTaxProfile
    year_end_month: int


@dataclass(frozen=True)
class PropertyTax:
    owner: str
    rented_fraction: int


type Effect = OrdinaryDeduction | TaxPayment | TaxTrueUp | MortgagePayment | PropertyTax | None


@dataclass
class Claim:
    cause_id: str
    obligation_type: str
    from_account: AccountRef
    to_account: AccountRef
    amount_due: int
    effect: Effect
    paid: bool = False


@dataclass
class Claims:
    month: int
    entries: list[Claim]

    def due(self, actor: str) -> list[tuple[ClaimId, Claim]]:
        return [
            (ClaimId(month=self.month, index=index), claim)
            for index, claim in enumerate(self.entries)
            if not claim.paid and claim.from_account.agent_id == actor
        ]


def assemble(
    scenario: PreparedScenario,
    market: MarketPath,
    month: int,
    properties: Sequence[PropertyState],
    installments: Sequence[MortgagePayment],
    liabilities: Sequence[TaxLiabilityState],
) -> Claims:
    active = {item.property_id: item for item in properties if item.active}
    claims = []
    scheduled = [claim for claim in scenario.obligations if claim.month == month]
    recurring = [
        claim
        for claim in scenario.recurring_obligations
        if claim.start_month <= month and (claim.end_month is None or month <= claim.end_month)
    ]
    for spec in (*scheduled, *recurring):
        if spec.property_id is not None and spec.property_id not in active:
            continue
        fraction = (
            active[spec.property_id].rented_fraction_ppb
            if spec.property_id is not None
            else spec.deductible_fraction_ppb
        )
        effect = OrdinaryDeduction(fraction) if spec.deduction_category == "ordinary" else None
        claims.append(
            Claim(
                f"{spec.obligation_id}_m{month}",
                spec.obligation_type,
                spec.from_account,
                spec.to_account,
                market.amount(spec.amount_due, month),
                effect,
            )
        )
    for installment in installments:
        terms = installment.terms
        claims.append(
            Claim(
                f"{terms.liability_id}_payment_m{month}",
                "mortgage_payment",
                terms.borrower,
                terms.lender,
                checked_count(installment.interest + installment.principal, "money addition"),
                installment,
            )
        )
    purchases = {purchase.property_id: purchase for purchase in scenario._scheduled_property_purchases}
    locations = {location.location_id: location for location in scenario.locations}
    for policy in scenario._property_tax_policies:
        if policy.property_id not in active:
            continue
        property_ = active[policy.property_id]
        if (
            property_.purchase_month >= month
            or policy.start_month > month
            or (policy.end_month is not None and month > policy.end_month)
        ):
            continue
        purchase = purchases[policy.property_id]
        location = locations[property_.location_id]
        rate = (
            location.annual_property_tax_rate_ppb if policy.annual_tax_rate_ppb is None else policy.annual_tax_rate_ppb
        )
        numerator = checked_wide(
            purchase.purchase_price * rate + location.annual_special_assessment * MONEY_FACTOR_SCALE, "property tax"
        )
        amount = checked_count(mul_div_wide(numerator, 1, 12 * MONEY_FACTOR_SCALE, "property tax"), "property tax")
        claims.append(
            Claim(
                f"{policy.property_id}_property_tax_m{month}",
                "property_tax",
                AccountRef(agent_id=policy.owner_agent_id, account_id=policy.from_account_id),
                AccountRef(agent_id=policy.tax_authority_agent_id, account_id=policy.tax_authority_account_id),
                amount,
                PropertyTax(policy.owner_agent_id, property_.rented_fraction_ppb),
            )
        )
    claims.extend(tax_claims(scenario.tax_profiles, liabilities, month))
    return Claims(month, claims)


def tax_claims(
    profiles: Sequence[PreparedTaxProfile], liabilities: Sequence[TaxLiabilityState], month: int
) -> list[Claim]:
    quarter = {3: 1, 5: 2, 8: 3}.get(month % 12)
    if quarter is None and not (month > 0 and month % 12 == 0):
        return []
    claims = []
    for profile in profiles:
        source = AccountRef(agent_id=profile.agent_id, account_id=profile.payment_account_id)
        destination = AccountRef(agent_id=profile.tax_authority_agent_id, account_id=profile.tax_authority_account_id)
        if quarter is not None:
            if profile.prior_year_tax <= 0:
                continue
            amount = mul_div(profile.prior_year_tax, 1, 4, "quarterly estimated tax")
            if amount:
                claims.append(
                    Claim(
                        f"{profile.agent_id}_estimated_tax_q{quarter}_y{month // 12}",
                        "estimated_tax",
                        source,
                        destination,
                        amount,
                        TaxPayment(profile),
                    )
                )
            continue
        tax_year = month // 12 - 1
        year_end = tax_year * 12 + 11
        actual = 0
        for liability in liabilities:
            if liability.active and liability.agent_id == profile.agent_id and liability.tax_year_end_month == year_end:
                actual = checked_count(actual + liability.amount_owed, "money addition")
        safe_harbor = min(profile.prior_year_tax, actual)
        previous_quarters = mul_div(profile.prior_year_tax, 3, 4, "first three estimated-tax quarters")
        q4 = max(0, safe_harbor - previous_quarters)
        if q4:
            claims.append(
                Claim(
                    f"{profile.agent_id}_estimated_tax_q4_y{tax_year}",
                    "estimated_tax",
                    source,
                    destination,
                    q4,
                    TaxPayment(profile),
                )
            )
        true_up = max(0, actual - safe_harbor)
        if true_up:
            claims.append(
                Claim(
                    f"{profile.agent_id}_tax_true_up_y{tax_year}",
                    "tax_true_up",
                    source,
                    destination,
                    true_up,
                    TaxTrueUp(profile, year_end),
                )
            )
    return claims
