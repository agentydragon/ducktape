"""Current-month contractual demands; registering one neither funds nor settles it."""

from dataclasses import dataclass
from enum import StrEnum

from finance.augur.sim import observations
from finance.augur.sim.actions import ClaimId
from finance.augur.sim.books import AccountRef, Record
from finance.augur.sim.ids import AgentId
from finance.augur.sim.mortgage import InstallmentDue, MortgagePayment
from finance.augur.sim.tax import PreparedTaxProfile


class ObligationType(StrEnum):
    """Closed set of `obligation_type` values that flow through dense engine event tables.

    Sim and product callers should use these enum members at construction sites and at
    filter sites in decoded `obligation_settlements` / `obligation_failures` frames.
    """

    CASH_SPEND = "cash_spend"
    OUTSIDE_RENT = "outside_rent"
    ESTIMATED_TAX = "estimated_tax"
    TAX_TRUE_UP = "tax_true_up"
    MORTGAGE_PAYMENT = "mortgage_payment"
    PROPERTY_TAX = "property_tax"
    HOA_DUES = "hoa_dues"
    HOMEOWNERS_INSURANCE = "homeowners_insurance"
    PROPERTY_MAINTENANCE = "property_maintenance"


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
    owner: AgentId
    rented_fraction: int


type Effect = OrdinaryDeduction | TaxPayment | TaxTrueUp | MortgagePayment | PropertyTax | None


class Demand(Record):
    """What a counterparty emits on `MonthOpened`; the ledger registers it as this month's claim on the payer."""

    cause_id: str
    obligation_type: str
    from_account: AccountRef
    to_account: AccountRef


class BillDue(observations.Claim):
    """A scheduled or recurring obligation, addressed to its payer."""


class AssessmentDue(observations.Claim):
    """An estimated-tax instalment or a year-end true-up, addressed to the taxpayer."""


class PropertyTaxDue(observations.Claim):
    """A property's monthly tax, addressed to its owner."""


type Due = BillDue | AssessmentDue | InstallmentDue | PropertyTaxDue


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

    def due(self, actor: AgentId) -> list[tuple[ClaimId, Claim]]:
        return [
            (ClaimId(month=self.month, index=index), claim)
            for index, claim in enumerate(self.entries)
            if not claim.paid and claim.from_account.agent_id == actor
        ]

    def dues(self, actor: AgentId) -> list[Due]:
        """This month's unpaid demands on `actor`, typed by what raised them."""
        dues: list[Due] = []
        for id_, claim in self.due(actor):
            kind: type[Due]
            match claim.effect:
                case MortgagePayment():
                    kind = InstallmentDue
                case TaxPayment() | TaxTrueUp():
                    kind = AssessmentDue
                case PropertyTax():
                    kind = PropertyTaxDue
                case OrdinaryDeduction() | None:
                    kind = BillDue
            dues.append(
                kind(
                    month=id_.month,
                    index=id_.index,
                    cause_id=claim.cause_id,
                    obligation_type=claim.obligation_type,
                    from_account=claim.from_account,
                    to_account=claim.to_account,
                    amount_due=claim.amount_due,
                )
            )
        return dues
