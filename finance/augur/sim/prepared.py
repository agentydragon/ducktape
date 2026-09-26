"""Owned, resolved simulation facts: exact money/quantity counts and supplied paths.

The declaration vocabulary a composed world takes. Underscored configured records
preserve existing consumers until their policies move to the common action session.
"""

from dataclasses import dataclass
from typing import Literal

from finance.augur.sim.books import AccountRef
from finance.augur.sim.jurisdictions import JurisdictionLevel
from finance.augur.sim.scenario import TransferDeductionCategory, TransferIncomeCategory
from finance.augur.sim.tlh import TlhAssumptions, TlhOpeningCohort


@dataclass(frozen=True, kw_only=True)
class PreparedAccount:
    account: AccountRef
    opening_balance: int


@dataclass(frozen=True, kw_only=True)
class PreparedHoldingPool:
    agent_id: str
    account_id: str
    asset_id: str
    quantity_scale: int


@dataclass(frozen=True, kw_only=True)
class PreparedFixedAmount:
    amount: int
    kind: Literal["fixed"] = "fixed"


@dataclass(frozen=True, kw_only=True)
class PreparedIndexedAmount:
    base_amount: int
    series_id: str
    base_month_index: int
    adjustment_period_months: int
    kind: Literal["series_indexed"] = "series_indexed"


type PreparedAmount = int | PreparedFixedAmount | PreparedIndexedAmount


@dataclass(frozen=True, kw_only=True)
class PreparedFlow:
    cause_id: str
    from_account: AccountRef
    to_account: AccountRef
    amount: PreparedAmount
    income_category: TransferIncomeCategory | None
    deduction_category: TransferDeductionCategory | None


@dataclass(frozen=True, kw_only=True)
class PreparedTransfer(PreparedFlow):
    month: int


@dataclass(frozen=True, kw_only=True)
class PreparedRecurringTransfer(PreparedFlow):
    start_month: int
    end_month: int | None


@dataclass(frozen=True, kw_only=True)
class PreparedPropertyCashflow(PreparedTransfer):
    property_id: str


@dataclass(frozen=True, kw_only=True)
class PreparedRecurringPropertyCashflow(PreparedRecurringTransfer):
    property_id: str


@dataclass(frozen=True, kw_only=True)
class PreparedClaim:
    obligation_id: str
    obligation_type: str
    from_account: AccountRef
    to_account: AccountRef
    amount_due: PreparedAmount
    property_id: str | None
    deduction_category: TransferDeductionCategory | None
    deductible_fraction_ppb: int


@dataclass(frozen=True, kw_only=True)
class PreparedObligation(PreparedClaim):
    month: int


@dataclass(frozen=True, kw_only=True)
class PreparedRecurringObligation(PreparedClaim):
    start_month: int
    end_month: int | None


@dataclass(frozen=True, kw_only=True)
class PreparedLot:
    lot_id: str
    agent_id: str
    account_id: str
    asset_id: str
    purchase_month: int
    quantity_scale: int
    units: int
    basis: int


@dataclass(frozen=True, kw_only=True)
class PreparedIndexedCoupon:
    annual_rate_ppb: int
    kind: Literal["indexed"] = "indexed"


@dataclass(frozen=True, kw_only=True)
class PreparedBond:
    bond_id: str
    agent_id: str
    account_id: str
    issuer_jurisdiction_id: str | None
    face_value: int
    purchase_price: int
    coupon: PreparedFixedAmount | PreparedIndexedCoupon
    coupon_period_months: int
    purchase_month_index: int
    maturity_month_index: int


@dataclass(frozen=True, kw_only=True)
class PreparedDistributionSlice:
    fraction_ppb: int
    issuer_jurisdiction_id: str | None


@dataclass(frozen=True, kw_only=True)
class PreparedDistribution:
    agent_id: str
    holding_account_id: str
    asset_id: str
    to_account_id: str
    tax_character: tuple[PreparedDistributionSlice, ...]


@dataclass(frozen=True, kw_only=True)
class PreparedJurisdiction:
    jurisdiction_id: str
    level: JurisdictionLevel


@dataclass(frozen=True, kw_only=True)
class PreparedLocation:
    location_id: str
    display_name: str
    jurisdiction_ids: tuple[str, ...]
    annual_property_tax_rate_ppb: int
    annual_special_assessment: int


@dataclass(frozen=True, kw_only=True)
class _SecuritySleeveTarget:
    """Lots of one security across the policy's source accounts, traded in whole units at its quote."""

    asset_id: str
    weight: int
    quantity_scale: int


@dataclass(frozen=True, kw_only=True)
class _ManagedSleeveTarget:
    """One managed portfolio, sized in money: it has a value but no units and no unit price."""

    portfolio_id: str
    weight: int


type _SleeveTarget = _SecuritySleeveTarget | _ManagedSleeveTarget


@dataclass(frozen=True, kw_only=True)
class _AllocationPolicy:
    agent_id: str
    account_id: str
    source_account_ids: tuple[str, ...]
    sleeves: tuple[_SleeveTarget, ...]
    cash_floor: PreparedAmount
    cash_ceiling: PreparedAmount
    cause_id_prefix: str
    allow_purchases: bool
    rebalance_tolerance_ppb: int | None


@dataclass(frozen=True, kw_only=True)
class _TenderPolicy:
    owner_agent_id: str
    proceeds_account_id: str
    liquid_net_worth_floor: PreparedAmount


@dataclass(frozen=True, kw_only=True)
class PreparedTlhPortfolio:
    portfolio_id: str
    owner_agent_id: str
    account_id: str
    asset_id: str
    initial_cohorts: tuple[TlhOpeningCohort, ...]
    assumptions: TlhAssumptions


@dataclass(frozen=True, kw_only=True)
class _MortgageFinancing:
    liability_id: str
    lender_agent_id: str
    lender_account_id: str
    principal: int
    annual_interest_rate_ppb: int
    term_months: int


@dataclass(frozen=True, kw_only=True)
class _PropertyPurchase:
    month: int
    cause_id: str
    property_id: str
    location_id: str
    buyer_agent_id: str
    buyer_account_id: str
    seller_agent_id: str
    seller_account_id: str
    purchase_price: int
    down_payment: int
    buyer_closing_cost: int
    rented_fraction_ppb: int
    land_value_fraction_ppb: int
    mortgage: _MortgageFinancing | None


@dataclass(frozen=True, kw_only=True)
class _PrimaryResidence:
    agent_id: str
    property_id: str


@dataclass(frozen=True, kw_only=True)
class _PrimaryResidenceEvent:
    month: int
    agent_id: str
    property_id: str | None


@dataclass(frozen=True, kw_only=True)
class _RentedFraction:
    month: int
    property_id: str
    rented_fraction_ppb: int


@dataclass(frozen=True, kw_only=True)
class _CapitalImprovement:
    month: int
    property_id: str
    amount: int
    description: str


@dataclass(frozen=True, kw_only=True)
class _PropertySale:
    month: int
    property_id: str
    closing_cost_ppb: int


@dataclass(frozen=True, kw_only=True)
class _MortgageInterestDeduction:
    liability_id: str
    owner_agent_id: str
    debt_class: Literal["acquisition", "home_equity"]
    per_jurisdiction_principal_cap: dict[str, int]


@dataclass(frozen=True, kw_only=True)
class _PropertyTax:
    property_id: str
    owner_agent_id: str
    from_account_id: str
    tax_authority_agent_id: str
    tax_authority_account_id: str
    annual_tax_rate_ppb: int | None
    start_month: int
    end_month: int | None


@dataclass(frozen=True, kw_only=True)
class _SaltCap:
    effective_year_index: int
    cap: int


@dataclass(frozen=True, kw_only=True)
class _SaltDeduction:
    """Federal SALT itemized deduction (Schedule A) for one enrolled taxpayer.

    Each of the profile's jurisdictions other than `federal_jurisdiction_id` contributes the
    income tax it accrued this calendar year, alongside property tax paid; the total is capped
    by the latest `cap_schedule` entry in effect (year index 0-based from the horizon's start;
    an empty schedule is uncapped) and stacks with mortgage interest. Not modeled: the AGI
    phase-out of the cap, the sales-tax election, and deducting prior-year true-ups in the year
    they are paid.
    """

    profile_id: str
    federal_jurisdiction_id: str
    cap_schedule: tuple[_SaltCap, ...]


@dataclass(frozen=True, kw_only=True)
class PreparedSeries:
    """One supplied integer path population in original rollout order."""

    series_id: str
    snapshots: int
    values: tuple[int, ...]
