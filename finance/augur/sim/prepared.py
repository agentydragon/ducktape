"""Owned, resolved simulation facts: exact money/quantity counts and supplied paths.

Preparation constructs these records directly. Native and file codecs serialize
them at I/O; no mutable wire document is retained. Underscored configured records
preserve existing consumers until their policies move to the common action session.
"""

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, PlainSerializer

from finance.augur.sim.books import AccountRef
from finance.augur.sim.compiler.income_sources import income_source_wire_id
from finance.augur.sim.compiler.tax import PreparedTaxProfile
from finance.augur.sim.jurisdictions import JurisdictionLevel
from finance.augur.sim.scenario import InterestIncome, OrdinaryIncome, TransferDeductionCategory, TransferIncomeCategory


def _income_source(value: object) -> TransferIncomeCategory:
    if isinstance(value, OrdinaryIncome | InterestIncome):
        return value
    if value == "ordinary":
        return OrdinaryIncome()
    if isinstance(value, str) and value.startswith("interest:"):
        issuer = value.removeprefix("interest:")
        return InterestIncome(issuer_jurisdiction_id=None if issuer == "corporate" else issuer)
    raise ValueError("income source must be ordinary or interest:<issuer>")


type _SerializedIncome = Annotated[
    TransferIncomeCategory, BeforeValidator(_income_source), PlainSerializer(income_source_wire_id, return_type=str)
]


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
    from_account: Annotated[AccountRef, Field(alias="from")]
    to_account: Annotated[AccountRef, Field(alias="to")]
    amount: PreparedAmount
    income_category: _SerializedIncome | None
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
    from_account: Annotated[AccountRef, Field(alias="from")]
    to_account: Annotated[AccountRef, Field(alias="to")]
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
class _ScheduledSale:
    month: int
    cause_id: str
    agent_id: str
    account_id: str
    asset_id: str
    units: int
    proceeds_account_id: str


@dataclass(frozen=True, kw_only=True)
class _SleeveTarget:
    asset_id: str
    weight: int
    quantity_scale: int


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
class _HarvestPolicy:
    owner_agent_id: str
    account_id: str
    asset_id: str
    peak_annual_yield_ppb: int
    floor_annual_yield_ppb: int
    maturity_decay_exponent_ppb: int
    drawdown_sensitivity_ppb: int
    short_term_fraction_ppb: int


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
    profile_id: str
    federal_jurisdiction_id: str
    cap_schedule: tuple[_SaltCap, ...]


@dataclass(frozen=True, kw_only=True)
class PreparedScenario:
    """Initial books, due cashflows and resolved rules; configured strategies stay private."""

    horizon_months: int
    accounts: tuple[PreparedAccount, ...]
    holding_pools: tuple[PreparedHoldingPool, ...]
    jurisdictions: tuple[PreparedJurisdiction, ...]
    locations: tuple[PreparedLocation, ...]
    scheduled_transfers: tuple[PreparedTransfer, ...]
    recurring_transfers: tuple[PreparedRecurringTransfer, ...]
    scheduled_property_cashflows: tuple[PreparedPropertyCashflow, ...]
    recurring_property_cashflows: tuple[PreparedRecurringPropertyCashflow, ...]
    obligations: tuple[PreparedObligation, ...]
    recurring_obligations: tuple[PreparedRecurringObligation, ...]
    initial_lots: tuple[PreparedLot, ...]
    initial_bonds: tuple[PreparedBond, ...]
    tax_profiles: tuple[PreparedTaxProfile, ...]
    income_sources: tuple[_SerializedIncome, ...]
    distributions: tuple[PreparedDistribution, ...]
    _scheduled_sales: Annotated[tuple[_ScheduledSale, ...], Field(alias="scheduled_sales")]
    _target_allocation_policies: Annotated[tuple[_AllocationPolicy, ...], Field(alias="target_allocation_policies")]
    _private_equity_tender_policies: Annotated[tuple[_TenderPolicy, ...], Field(alias="private_equity_tender_policies")]
    _harvest_policies: Annotated[tuple[_HarvestPolicy, ...], Field(alias="harvest_policies")]
    _scheduled_property_purchases: Annotated[tuple[_PropertyPurchase, ...], Field(alias="scheduled_property_purchases")]
    _initial_primary_residences: Annotated[tuple[_PrimaryResidence, ...], Field(alias="initial_primary_residences")]
    _primary_residence_events: Annotated[tuple[_PrimaryResidenceEvent, ...], Field(alias="primary_residence_events")]
    _property_rented_fraction_events: Annotated[
        tuple[_RentedFraction, ...], Field(alias="property_rented_fraction_events")
    ]
    _capital_improvement_events: Annotated[tuple[_CapitalImprovement, ...], Field(alias="capital_improvement_events")]
    _property_sales: Annotated[tuple[_PropertySale, ...], Field(alias="property_sales")]
    _mortgage_interest_deduction_policies: Annotated[
        tuple[_MortgageInterestDeduction, ...], Field(alias="mortgage_interest_deduction_policies")
    ]
    _property_tax_policies: Annotated[tuple[_PropertyTax, ...], Field(alias="property_tax_policies")]
    _federal_salt_deduction_policies: Annotated[
        tuple[_SaltDeduction, ...], Field(alias="federal_salt_deduction_policies")
    ]

    @property
    def has_property_purchases(self) -> bool:
        return bool(self._scheduled_property_purchases)


@dataclass(frozen=True, kw_only=True)
class PreparedSeries:
    """One supplied integer path population in original rollout order."""

    series_id: str
    snapshots: int
    values: tuple[int, ...]


@dataclass(frozen=True, kw_only=True)
class CompiledRun:
    """The sole prepared authority; source declarations and wire documents are not retained."""

    currency_code: str
    currency_quantum: str
    rollout_count: int
    scenario: PreparedScenario
    series: tuple[PreparedSeries, ...]
    _schema_version: Annotated[Literal[14], Field(alias="schema_version")] = 14
