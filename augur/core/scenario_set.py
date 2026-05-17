from __future__ import annotations

from collections import Counter
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import Field, NonNegativeFloat, NonNegativeInt, PositiveFloat, PositiveInt, model_validator

from augur.core.accounting import (
    BalanceSnapshot,
    ChartAccount,
    JournalEntry,
    LiabilityState,
    LotDisposition,
    Posting,
    TaxLot,
)
from augur.core.local_regulation import LocalRegulation, TaxRegime
from augur.core.provenance import ProjectionRun
from augur.core.schemas import ApiModel, ColumnarTable, Percentage


class EventType(StrEnum):
    PROPERTY_PURCHASE = "property_purchase"
    PROPERTY_SALE = "property_sale"
    MORTGAGE_ORIGINATION = "mortgage_origination"
    MOVE_RESIDENCE = "move_residence"
    START_RENTAL = "start_rental"
    STOP_RENTAL = "stop_rental"
    PORTFOLIO_TRADE = "portfolio_trade"
    PRIVATE_EQUITY_IPO = "private_equity_ipo"
    PRIVATE_EQUITY_ACQUISITION = "private_equity_acquisition"
    SPECIAL_ASSESSMENT = "special_assessment"


class PolicyType(StrEnum):
    CHECKING_FLOOR_SELL_PUBLIC_STOCK = "checking_floor_sell_public_stock"
    PRIVATE_EQUITY_SALE = "private_equity_sale"
    PARTNER_EQUITY_ACCRUAL = "partner_equity_accrual"
    MONTHLY_SPEND = "monthly_spend"


class PrivateEquitySaleRuleType(StrEnum):
    FIXED_AMOUNT_ON_OPPORTUNITY = "fixed_amount_on_opportunity"
    LIQUID_NET_WORTH_FLOOR = "liquid_net_worth_floor"


class PrivateEquitySaleProceedsDestination(StrEnum):
    CASH = "cash"
    GENERIC_SP500_STOCK = "generic_sp500_stock"


class LiquidityReserveRuleType(StrEnum):
    FIXED = "fixed"
    PROJECTED_DEFICITS = "projected_deficits"


class ActionType(StrEnum):
    """Discriminator for user-visible trajectory actions.

    Restricted to sale-class commands: the system-emitted accounting moves
    (mortgage settlement, partner contributions, partner-equity accruals,
    monthly spend) are derivable from ledger postings, balance snapshots, and
    accounting details — the canonical detail surface — so they are not
    surfaced as separate action rows.
    """

    SELL_SP500 = "sell_sp500"
    SELL_CRYPTO = "sell_crypto"
    SELL_PRIVATE_EQUITY = "sell_private_equity"
    SETTLE_PROPERTY_SALE = "settle_property_sale"


class PolicyDecisionType(StrEnum):
    MONTHLY_SPEND = "monthly_spend"
    SELL_PUBLIC_STOCK = "sell_public_stock"
    SELL_CRYPTO = "sell_crypto"
    PRIVATE_EQUITY_SALE = "private_equity_sale"
    PARTNER_CONTRIBUTION = "partner_contribution"


class PrivateEquitySaleDecisionReason(StrEnum):
    NO_SALE_OPPORTUNITY = "no_sale_opportunity"
    POLICY_NOT_TRIGGERED = "policy_not_triggered"
    SALE_REQUESTED = "sale_requested"


class MarketObservationType(StrEnum):
    MARKET_PATH = "market_path"
    PRIVATE_EQUITY_SALE_OPPORTUNITY = "private_equity_sale_opportunity"


class RolloutStatusType(StrEnum):
    ACTIVE = "active"
    CASH_NEGATIVE = "cash_negative"
    FAILED = "failed"


class AccountingDetailType(StrEnum):
    PROPERTY_SALE_BASIS_GAIN = "property_sale_basis_gain"
    TAX_PAYMENT_ALLOCATION = "tax_payment_allocation"


class ReportMetric(StrEnum):
    MONTH_INDEX = "month_index"
    CASH_USD = "cash_usd"
    GENERIC_SP500_VALUE_USD = "generic_sp500_value_usd"
    GENERIC_SP500_SALE_USD = "generic_sp500_sale_usd"
    GENERIC_SP500_SALE_BASIS_USD = "generic_sp500_sale_basis_usd"
    GENERIC_SP500_SALE_GAIN_USD = "generic_sp500_sale_gain_usd"
    GENERIC_SP500_SALE_TAX_USD = "generic_sp500_sale_tax_usd"
    CRYPTO_VALUE_USD = "crypto_value_usd"
    CRYPTO_SALE_USD = "crypto_sale_usd"
    CRYPTO_SALE_BASIS_USD = "crypto_sale_basis_usd"
    CRYPTO_SALE_GAIN_USD = "crypto_sale_gain_usd"
    CHECKING_FLOOR_ACTION_USD = "checking_floor_action_usd"
    CHECKING_FLOOR_SHORTFALL_USD = "checking_floor_shortfall_usd"
    PRIVATE_EQUITY_VALUE_USD = "private_equity_value_usd"
    PRIVATE_EQUITY_SALE_OPPORTUNITY_VALUE_USD = "private_equity_sale_opportunity_value_usd"
    PRIVATE_EQUITY_SALE_USD = "private_equity_sale_usd"
    PRIVATE_EQUITY_SALE_BASIS_USD = "private_equity_sale_basis_usd"
    PRIVATE_EQUITY_SALE_TAX_USD = "private_equity_sale_tax_usd"
    RENTAL_INCOME_TAX_USD = "rental_income_tax_usd"
    FEDERAL_INCOME_TAX_USD = "federal_income_tax_usd"
    CALIFORNIA_INCOME_TAX_USD = "california_income_tax_usd"
    TOTAL_INCOME_TAX_USD = "total_income_tax_usd"
    PRIVATE_EQUITY_SALE_OPPORTUNITY_EVENT = "private_equity_sale_opportunity_event"
    PROPERTY_VALUE_USD = "property_value_usd"
    MORTGAGE_BALANCE_USD = "mortgage_balance_usd"
    MORTGAGE_INTEREST_USD = "mortgage_interest_usd"
    MORTGAGE_PRINCIPAL_USD = "mortgage_principal_usd"
    MORTGAGE_PAYMENT_USD = "mortgage_payment_usd"
    PROPERTY_TAX_USD = "property_tax_usd"
    HOA_USD = "hoa_usd"
    INSURANCE_USD = "insurance_usd"
    MAINTENANCE_USD = "maintenance_usd"
    RENTAL_INCOME_USD = "rental_income_usd"
    RENTAL_MANAGEMENT_FEE_USD = "rental_management_fee_usd"
    RENTAL_LEASING_FEE_USD = "rental_leasing_fee_usd"
    PROPERTY_CARRYING_COST_USD = "property_carrying_cost_usd"
    NET_PROPERTY_CASH_FLOW_USD = "net_property_cash_flow_usd"
    PURCHASE_CLOSING_COST_USD = "purchase_closing_cost_usd"
    SALE_CLOSING_COST_USD = "sale_closing_cost_usd"
    PROPERTY_DEPRECIATION_USD = "property_depreciation_usd"
    CUMULATIVE_PROPERTY_DEPRECIATION_USD = "cumulative_property_depreciation_usd"
    PROPERTY_SALE_GROSS_USD = "property_sale_gross_usd"
    PROPERTY_SALE_NET_PROCEEDS_USD = "property_sale_net_proceeds_usd"
    PROPERTY_SALE_TAX_USD = "property_sale_tax_usd"
    PROPERTY_SALE_DEBT_PAYOFF_USD = "property_sale_debt_payoff_usd"
    PROPERTY_SALE_ADJUSTED_BASIS_USD = "property_sale_adjusted_basis_usd"
    REALIZED_PROPERTY_GAIN_USD = "realized_property_gain_usd"
    PROPERTY_SALE_CAPITAL_GAIN_USD = "property_sale_capital_gain_usd"
    PROPERTY_SALE_CAPITAL_GAIN_EXCLUSION_USD = "property_sale_capital_gain_exclusion_usd"
    TAXABLE_PROPERTY_CAPITAL_GAIN_USD = "taxable_property_capital_gain_usd"
    TAXABLE_PROPERTY_GAIN_USD = "taxable_property_gain_usd"
    DEPRECIATION_RECAPTURE_USD = "depreciation_recapture_usd"
    NET_PROPERTY_SALE_CASH_FLOW_USD = "net_property_sale_cash_flow_usd"
    HOME_EQUITY_USD = "home_equity_usd"
    OWNER_HOME_EQUITY_CLAIM_USD = "owner_home_equity_claim_usd"
    PARTNER_HOME_EQUITY_CLAIM_USD = "partner_home_equity_claim_usd"
    PARTNER_CONTRIBUTION_USD = "partner_contribution_usd"
    PARTNER_CONTRIBUTION_USED_USD = "partner_contribution_used_usd"
    PARTNER_UNALLOCATED_EXCESS_USD = "partner_unallocated_excess_usd"
    PARTNER_HOUSE_COSTS_USD = "partner_house_costs_usd"
    PARTNER_PRINCIPAL_CREDIT_USD = "partner_principal_credit_usd"
    OWNER_PRINCIPAL_CREDIT_USD = "owner_principal_credit_usd"
    PARTNER_HOUSE_COST_SHARE = "partner_house_cost_share"
    PARTNER_EQUITY_LEDGER_USD = "partner_equity_ledger_usd"
    OWNER_EQUITY_LEDGER_USD = "owner_equity_ledger_usd"
    PARTNER_OWNERSHIP_PCT = "partner_ownership_pct"
    LIQUID_NET_WORTH_USD = "liquid_net_worth_usd"
    NET_WORTH_USD = "net_worth_usd"
    PARTNER_PRESENT = "partner_present"
    MONTHLY_SPEND_USD = "monthly_spend_usd"


class TaxPaymentTiming(StrEnum):
    YEAR_END = "year_end"


class ObligationType(StrEnum):
    ANNUAL_TAX_PAYMENT = "annual_tax_payment"
    ESTIMATED_TAX_PAYMENT = "estimated_tax_payment"
    MORTGAGE_PAYMENT = "mortgage_payment"
    PROPERTY_TAX = "property_tax"
    HOA_DUES = "hoa_dues"
    INSURANCE_PREMIUM = "insurance_premium"
    MAINTENANCE = "maintenance"
    OUTSIDE_RENT = "outside_rent"
    SPECIAL_ASSESSMENT = "special_assessment"
    PARTNER_CONTRIBUTION = "partner_contribution"


class ObligationStatus(StrEnum):
    PAID = "paid"
    PARTIALLY_PAID = "partially_paid"
    UNPAID = "unpaid"


class FundingDecisionType(StrEnum):
    USE_CASH = "use_cash"
    SELL_PUBLIC_STOCK = "sell_public_stock"
    SELL_CRYPTO = "sell_crypto"
    UNFUNDED = "unfunded"


class FundingSourceType(StrEnum):
    CASH_ACCOUNT = "cash_account"
    PUBLIC_MARKET_ASSET = "public_market_asset"
    CRYPTO_ASSET = "crypto_asset"
    UNFUNDED = "unfunded"


class SettlementStatus(StrEnum):
    PAID = "paid"
    PARTIALLY_PAID = "partially_paid"
    UNPAID = "unpaid"


class FailureEventType(StrEnum):
    UNSETTLED_OBLIGATION = "unsettled_obligation"


class ActorRole(StrEnum):
    PRIMARY_OWNER = "primary_owner"
    EQUITY_BUILDING_OCCUPANT = "equity_building_occupant"
    TENANT = "tenant"
    LANDLORD = "landlord"


class AccountType(StrEnum):
    CHECKING = "checking"
    TAXABLE_BROKERAGE = "taxable_brokerage"
    ESCROW = "escrow"
    CRYPTO_EXCHANGE = "crypto_exchange"


class AssetType(StrEnum):
    GENERIC_SP500_STOCK = "generic_sp500_stock"
    CRYPTO = "crypto"
    PRIVATE_EQUITY = "private_equity"


PropertyId = str


class OccupancyMode(StrEnum):
    OWNER_LIVES_IN_PROPERTY = "owner_lives_in_property"
    OWNER_LIVES_IN_OTHER_OWNED_PROPERTY = "owner_lives_in_other_owned_property"
    # CLEANUP(2026-05-17): `OWNER_RENTS_ELSEWHERE` is not modeled — the
    #   engine has no per-month rent debit for the owner-as-tenant role and
    #   no rent-policy schema. Plan C slice 3 (`augur/plans/roadmap.md`)
    #   reserves `ObligationType.OUTSIDE_RENT` for the moment a rent policy
    #   lands and starts emitting the obligation. Remove this comment when
    #   that slice ships.
    OWNER_RENTS_ELSEWHERE = "owner_rents_elsewhere"
    NO_OWNER_OCCUPANCY = "no_owner_occupancy"


class RentalMode(StrEnum):
    NOT_RENTED = "not_rented"
    RENT_ROOMS_WHILE_OWNER_LIVES_THERE = "rent_rooms_while_owner_lives_there"
    RENT_WHOLE_PROPERTY = "rent_whole_property"
    TRANSITION_TO_WHOLE_PROPERTY_RENTAL = "transition_to_whole_property_rental"


class TaxFilingStatus(StrEnum):
    SINGLE = "single"
    MARRIED_FILING_JOINTLY = "married_filing_jointly"
    MARRIED_FILING_SEPARATELY = "married_filing_separately"
    HEAD_OF_HOUSEHOLD = "head_of_household"


class FinancingMode(StrEnum):
    CASH = "cash"
    FIXED_30 = "fixed_30"
    FIXED_15 = "fixed_15"
    CUSTOM = "custom"


class Actor(ApiModel):
    actor_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_\-]*$")
    label: str
    role: ActorRole


class _EventBase(ApiModel):
    event_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_\-]*$")
    month_index: NonNegativeInt
    actor_id: str | None = None
    property_id: PropertyId | None = None
    amount_usd: float | None = None
    description: str | None = None


class PropertyPurchaseEvent(_EventBase):
    event_type: Literal[EventType.PROPERTY_PURCHASE] = EventType.PROPERTY_PURCHASE
    hoa_monthly_usd: NonNegativeFloat | None = None


class PropertySaleEvent(_EventBase):
    event_type: Literal[EventType.PROPERTY_SALE] = EventType.PROPERTY_SALE


class MortgageOriginationEvent(_EventBase):
    event_type: Literal[EventType.MORTGAGE_ORIGINATION] = EventType.MORTGAGE_ORIGINATION


class MoveResidenceEvent(_EventBase):
    event_type: Literal[EventType.MOVE_RESIDENCE] = EventType.MOVE_RESIDENCE


class StartRentalEvent(_EventBase):
    event_type: Literal[EventType.START_RENTAL] = EventType.START_RENTAL


class StopRentalEvent(_EventBase):
    event_type: Literal[EventType.STOP_RENTAL] = EventType.STOP_RENTAL


class PortfolioTradeEvent(_EventBase):
    event_type: Literal[EventType.PORTFOLIO_TRADE] = EventType.PORTFOLIO_TRADE


class PrivateEquityIpoEvent(_EventBase):
    event_type: Literal[EventType.PRIVATE_EQUITY_IPO] = EventType.PRIVATE_EQUITY_IPO


class PrivateEquityAcquisitionEvent(_EventBase):
    event_type: Literal[EventType.PRIVATE_EQUITY_ACQUISITION] = EventType.PRIVATE_EQUITY_ACQUISITION


class SpecialAssessmentEvent(_EventBase):
    """One-shot HOA / association special assessment due in `month_index`.

    Routes through the unified obligation pipeline as a `SPECIAL_ASSESSMENT`
    obligation. `amount_usd` is the assessment amount; if `actor_id` is unset
    the obligation is billed to the scenario's primary owner.
    """

    event_type: Literal[EventType.SPECIAL_ASSESSMENT] = EventType.SPECIAL_ASSESSMENT
    amount_usd: PositiveFloat


Event = Annotated[
    PropertyPurchaseEvent
    | PropertySaleEvent
    | MortgageOriginationEvent
    | MoveResidenceEvent
    | StartRentalEvent
    | StopRentalEvent
    | PortfolioTradeEvent
    | PrivateEquityIpoEvent
    | PrivateEquityAcquisitionEvent
    | SpecialAssessmentEvent,
    Field(discriminator="event_type"),
]


class _PolicyBase(ApiModel):
    policy_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_\-]*$")
    actor_id: str
    enabled: bool = True


class CheckingFloorSellPublicStockPolicy(_PolicyBase):
    """Sell from the agent's liquid asset preferences when checking cash dips below a floor.

    `sale_asset_preference` is an ordered tuple of asset types the policy will try to
    sell in order, exhausting each before falling through to the next. Default
    `(GENERIC_SP500_STOCK,)` preserves the policy's original SP500-only behavior.
    Putting `CRYPTO` in the preference lets the same policy fall through to crypto
    after SP500 is exhausted; the same obligation-funding step iterates the
    preference internally so the policy program order is unaffected.
    """

    policy_type: Literal[PolicyType.CHECKING_FLOOR_SELL_PUBLIC_STOCK] = PolicyType.CHECKING_FLOOR_SELL_PUBLIC_STOCK
    floor_usd: NonNegativeFloat = 0.0
    sale_amount_usd: NonNegativeFloat = 0.0
    sale_asset_preference: tuple[AssetType, ...] = (AssetType.GENERIC_SP500_STOCK,)

    @model_validator(mode="after")
    def _validate_sale_asset_preference(self) -> CheckingFloorSellPublicStockPolicy:
        if not self.sale_asset_preference:
            raise ValueError("sale_asset_preference must contain at least one asset type")
        seen: set[AssetType] = set()
        for asset_type in self.sale_asset_preference:
            if asset_type in seen:
                raise ValueError(f"sale_asset_preference contains duplicate {asset_type}")
            if asset_type not in (AssetType.GENERIC_SP500_STOCK, AssetType.CRYPTO):
                raise ValueError(
                    f"sale_asset_preference only supports GENERIC_SP500_STOCK and CRYPTO; got {asset_type}"
                )
            seen.add(asset_type)
        return self


class FixedAmountPrivateEquitySaleRule(ApiModel):
    sale_rule_type: Literal[PrivateEquitySaleRuleType.FIXED_AMOUNT_ON_OPPORTUNITY] = (
        PrivateEquitySaleRuleType.FIXED_AMOUNT_ON_OPPORTUNITY
    )
    amount_usd: PositiveFloat


class LiquidNetWorthFloorPrivateEquitySaleRule(ApiModel):
    sale_rule_type: Literal[PrivateEquitySaleRuleType.LIQUID_NET_WORTH_FLOOR] = (
        PrivateEquitySaleRuleType.LIQUID_NET_WORTH_FLOOR
    )
    min_liquid_net_worth_usd: NonNegativeFloat
    sale_amount_usd: PositiveFloat


PrivateEquitySaleRule = Annotated[
    FixedAmountPrivateEquitySaleRule | LiquidNetWorthFloorPrivateEquitySaleRule, Field(discriminator="sale_rule_type")
]


class PrivateEquitySalePolicy(_PolicyBase):
    """Sell private equity when market sale opportunities satisfy the policy rule."""

    policy_type: Literal[PolicyType.PRIVATE_EQUITY_SALE] = PolicyType.PRIVATE_EQUITY_SALE
    proceeds_destination: PrivateEquitySaleProceedsDestination = PrivateEquitySaleProceedsDestination.CASH
    sale_rule: PrivateEquitySaleRule


class PartnerEquityAccrualPolicy(_PolicyBase):
    """A partner contributes monthly toward a property the primary owner holds, accruing
    ownership share in proportion to principal credit."""

    policy_type: Literal[PolicyType.PARTNER_EQUITY_ACCRUAL] = PolicyType.PARTNER_EQUITY_ACCRUAL
    property_id: PropertyId | None = None
    base_monthly_payment_usd: NonNegativeFloat = 0.0
    grow_with_inflation: bool = True
    payment_growth_annual_pct: NonNegativeFloat = 0.0
    occupied_months: NonNegativeInt | None = None
    freeze_ownership_after_month: NonNegativeInt | None = None


class MonthlySpendPolicy(_PolicyBase):
    """Agent spends a fixed amount each month from checking (e.g. living expenses).

    When `inflation_adjusted` is true, the spend grows with the market
    bundle's inflation multipliers."""

    policy_type: Literal[PolicyType.MONTHLY_SPEND] = PolicyType.MONTHLY_SPEND
    monthly_spend_usd: NonNegativeFloat
    inflation_adjusted: bool = False


Policy = Annotated[
    CheckingFloorSellPublicStockPolicy | PrivateEquitySalePolicy | PartnerEquityAccrualPolicy | MonthlySpendPolicy,
    Field(discriminator="policy_type"),
]


class _TraceBase(ApiModel):
    rollout_index: NonNegativeInt
    month_index: NonNegativeInt
    path_set_id: str | None = None
    exogenous_path_id: str | None = None
    scenario_input_id: str | None = None
    projection_trajectory_id: str | None = None


class _ActionBase(_TraceBase):
    actor_id: str
    policy_id: str


class SellSp500Action(_ActionBase):
    action_type: Literal[ActionType.SELL_SP500] = ActionType.SELL_SP500
    amount_usd: float
    after_tax_proceeds_usd: float
    basis_usd: float
    gain_usd: float
    tax_usd: float
    shortfall_usd: float


class SellCryptoAction(_ActionBase):
    action_type: Literal[ActionType.SELL_CRYPTO] = ActionType.SELL_CRYPTO
    source_asset_id: str
    asset_symbol: str
    amount_usd: float
    quantity_sold: float
    basis_usd: float
    gain_usd: float
    shortfall_usd: float


class SellPrivateEquityAction(_ActionBase):
    action_type: Literal[ActionType.SELL_PRIVATE_EQUITY] = ActionType.SELL_PRIVATE_EQUITY
    event_id: str | None = None
    event_type: EventType | None = None
    opportunity_id: str | None = None
    opportunity_cause_id: str
    amount_usd: float
    after_tax_proceeds_usd: float
    basis_usd: float
    taxable_gain_usd: float
    estimated_tax_usd: float
    units_sold: float
    sold_fraction: float
    proceeds_destination: AccountType | AssetType


class SettlePropertySaleAction(_ActionBase):
    action_type: Literal[ActionType.SETTLE_PROPERTY_SALE] = ActionType.SETTLE_PROPERTY_SALE
    event_id: str
    event_type: Literal[EventType.PROPERTY_SALE] = EventType.PROPERTY_SALE
    property_id: PropertyId
    gross_sale_usd: float
    selling_cost_usd: float
    debt_payoff_usd: float
    adjusted_basis_usd: float
    realized_gain_usd: float
    depreciation_recapture_usd: float
    capital_gain_usd: float
    capital_gain_exclusion_usd: float
    taxable_capital_gain_usd: float
    taxable_gain_usd: float
    tax_usd: float
    net_proceeds_usd: float
    proceeds_destination: AccountType = AccountType.CHECKING


Action = Annotated[
    SellSp500Action | SellCryptoAction | SellPrivateEquityAction | SettlePropertySaleAction,
    Field(discriminator="action_type"),
]


class _PolicyDecisionBase(_TraceBase):
    actor_id: str
    policy_id: str
    policy_sequence_index: NonNegativeInt


class MonthlySpendDecision(_PolicyDecisionBase):
    decision_type: Literal[PolicyDecisionType.MONTHLY_SPEND] = PolicyDecisionType.MONTHLY_SPEND
    amount_usd: float
    inflation_multiplier: float = 1.0


class SellPublicStockDecision(_PolicyDecisionBase):
    decision_type: Literal[PolicyDecisionType.SELL_PUBLIC_STOCK] = PolicyDecisionType.SELL_PUBLIC_STOCK
    asset_type: Literal[AssetType.GENERIC_SP500_STOCK] = AssetType.GENERIC_SP500_STOCK
    requested_amount_usd: float
    current_cash_usd: float
    target_cash_floor_usd: float | None = None


class SellCryptoDecision(_PolicyDecisionBase):
    decision_type: Literal[PolicyDecisionType.SELL_CRYPTO] = PolicyDecisionType.SELL_CRYPTO
    asset_type: Literal[AssetType.CRYPTO] = AssetType.CRYPTO
    source_asset_id: str
    requested_amount_usd: float
    current_cash_usd: float
    target_cash_floor_usd: float | None = None


class PrivateEquitySaleDecision(_PolicyDecisionBase):
    decision_type: Literal[PolicyDecisionType.PRIVATE_EQUITY_SALE] = PolicyDecisionType.PRIVATE_EQUITY_SALE
    decision_reason: PrivateEquitySaleDecisionReason
    source_asset_id: str = Field(description="Private-equity holding the policy targeted.")
    sale_rule_type: PrivateEquitySaleRuleType = Field(
        description="Rule variant the policy applied to this opportunity (lets the trajectory view explain the reason)."
    )
    configured_sale_amount_usd: float = Field(
        description="Rule-configured sale amount in USD. Equal to amount_usd for fixed rules and sale_amount_usd for the liquid-net-worth-floor rule."
    )
    opportunity_id: str | None = None
    opportunity_cause_id: str
    requested_amount_usd: float
    sale_opportunity_value_usd: float
    private_equity_value_before_sale_usd: float
    liquid_net_worth_usd: float
    target_liquid_net_worth_floor_usd: float | None = None
    proceeds_destination: AccountType | AssetType


class PartnerContributionDecision(_PolicyDecisionBase):
    decision_type: Literal[PolicyDecisionType.PARTNER_CONTRIBUTION] = PolicyDecisionType.PARTNER_CONTRIBUTION
    recipient_actor_id: str
    requested_amount_usd: float
    property_id: PropertyId


PolicyDecision = Annotated[
    MonthlySpendDecision
    | SellPublicStockDecision
    | SellCryptoDecision
    | PrivateEquitySaleDecision
    | PartnerContributionDecision,
    Field(discriminator="decision_type"),
]


class _MarketObservationBase(_TraceBase):
    pass


class MarketPathObservation(_MarketObservationBase):
    observation_type: Literal[MarketObservationType.MARKET_PATH] = MarketObservationType.MARKET_PATH
    location_id: str | None = None
    inflation_multiplier: float
    sp500_multiplier: float
    private_equity_value_multiplier: float
    home_value_multiplier: float
    rent_multiplier: float
    mortgage_30y_rate_pct: float
    private_equity_sale_opportunity_event: bool


class PrivateEquitySaleOpportunityObservation(_MarketObservationBase):
    observation_type: Literal[MarketObservationType.PRIVATE_EQUITY_SALE_OPPORTUNITY] = (
        MarketObservationType.PRIVATE_EQUITY_SALE_OPPORTUNITY
    )
    source_asset_id: str = Field(description="Private-equity holding that produced this tender opportunity.")
    opportunity_id: str
    opportunity_cause_id: str
    sale_opportunity_value_usd: float
    private_equity_value_before_sale_usd: float


MarketObservation = Annotated[
    MarketPathObservation | PrivateEquitySaleOpportunityObservation, Field(discriminator="observation_type")
]


class _AccountingDetailBase(_TraceBase):
    actor_id: str
    policy_id: str | None = None
    event_id: str | None = None
    property_id: PropertyId | None = None


class PropertySaleBasisGainDetail(_AccountingDetailBase):
    detail_type: Literal[AccountingDetailType.PROPERTY_SALE_BASIS_GAIN] = AccountingDetailType.PROPERTY_SALE_BASIS_GAIN
    gross_sale_usd: float
    selling_cost_usd: float
    debt_payoff_usd: float
    adjusted_basis_usd: float
    realized_gain_usd: float
    depreciation_recapture_usd: float
    capital_gain_usd: float
    capital_gain_exclusion_usd: float
    taxable_capital_gain_usd: float
    taxable_gain_usd: float


class TaxPaymentAllocationDetail(_AccountingDetailBase):
    detail_type: Literal[AccountingDetailType.TAX_PAYMENT_ALLOCATION] = AccountingDetailType.TAX_PAYMENT_ALLOCATION
    tax_year_index: NonNegativeInt
    payment_timing: TaxPaymentTiming = TaxPaymentTiming.YEAR_END
    federal_income_tax_usd: float
    california_income_tax_usd: float
    total_income_tax_usd: float
    property_sale_tax_usd: float
    generic_sp500_sale_tax_usd: float
    private_equity_sale_tax_usd: float
    rental_income_tax_usd: float
    property_depreciation_recapture_usd: float
    taxable_property_capital_gain_usd: float
    generic_sp500_taxable_gain_usd: float
    private_equity_taxable_gain_usd: float
    net_rental_taxable_income_usd: float
    total_taxable_income_usd: float


AccountingDetail = Annotated[
    PropertySaleBasisGainDetail | TaxPaymentAllocationDetail, Field(discriminator="detail_type")
]


class Obligation(_TraceBase):
    obligation_id: str
    obligation_type: ObligationType
    actor_id: str
    creditor_id: str
    due_month_index: NonNegativeInt
    amount_due_usd: float
    amount_paid_usd: float
    unpaid_amount_usd: float
    status: ObligationStatus
    source_policy_id: str | None = None


class FundingDecision(_TraceBase):
    obligation_id: str
    decision_type: FundingDecisionType
    actor_id: str
    policy_id: str | None = None
    policy_sequence_index: NonNegativeInt | None = None
    source_type: FundingSourceType | None = None
    source_account_id: str | None = None
    source_account_type: AccountType | None = None
    source_asset_id: str | None = None
    source_asset_type: AssetType | None = None
    available_cash_usd: float
    requested_cash_usd: float
    requested_sale_usd: float = 0.0
    funded_cash_usd: float = 0.0
    shortfall_usd: float = 0.0


class SettlementResult(_TraceBase):
    obligation_id: str
    obligation_type: ObligationType
    actor_id: str
    status: SettlementStatus
    amount_due_usd: float
    amount_paid_usd: float
    unpaid_amount_usd: float


class FailureEvent(_TraceBase):
    failure_event_id: str
    failure_event_type: FailureEventType
    obligation_id: str
    actor_id: str
    unpaid_amount_usd: float


class ReportSpec(ApiModel):
    percentiles: tuple[float, ...] = (5, 25, 50, 75, 95)
    include_monthly_columns: bool = True

    @model_validator(mode="after")
    def _percentiles_in_range(self) -> ReportSpec:
        out_of_range = [value for value in self.percentiles if value < 0 or value > 100]
        if out_of_range:
            raise ValueError(f"percentiles must be in [0, 100]: {out_of_range}")
        return self


class TaxProfile(ApiModel):
    filing_status: TaxFilingStatus = TaxFilingStatus.SINGLE
    annual_ordinary_income_usd: NonNegativeFloat = 0
    federal_standard_deduction_usd: NonNegativeFloat | None = None
    california_standard_deduction_usd: NonNegativeFloat | None = None


class TransactionCosts(ApiModel):
    closing_cost_buy_pct: Percentage = 2.5
    closing_cost_sell_pct: Percentage = 6.5


class PropertyAssumptions(ApiModel):
    insurance_annual_usd: NonNegativeFloat = 1800
    maintenance_pct: Percentage = 1
    depreciable_basis_pct: Percentage = 80


class PropertySelection(ApiModel):
    property_id: PropertyId | None = None
    location_id: str | None = None
    purchase_price_usd: NonNegativeFloat | None = None
    tax_regime: TaxRegime | None = None
    local_regulation: LocalRegulation | None = None


class Financing(ApiModel):
    financing_mode: FinancingMode = FinancingMode.FIXED_30
    down_payment_pct: NonNegativeFloat = 25
    mortgage_rate_pct: NonNegativeFloat | None = None
    mortgage_term_years: PositiveInt | None = None
    credit_score: NonNegativeInt | None = None
    loan_amount_usd: NonNegativeFloat | None = None


class OccupancyPlan(ApiModel):
    occupancy_mode: OccupancyMode = OccupancyMode.OWNER_LIVES_IN_PROPERTY
    owner_residence_property_id: PropertyId | None = None
    start_month: NonNegativeInt = 0
    end_month: NonNegativeInt | None = None

    @model_validator(mode="after")
    def _end_after_start(self) -> OccupancyPlan:
        if self.end_month is not None and self.end_month < self.start_month:
            raise ValueError("end_month must be greater than or equal to start_month")
        return self


class _RentalPlanBase(ApiModel):
    start_month: NonNegativeInt | None = None
    end_month: NonNegativeInt | None = None
    monthly_rent_usd: NonNegativeFloat | None = None
    rooms_rented: NonNegativeInt = 0
    room_rent_monthly_usd: NonNegativeFloat | None = None
    vacancy_pct: NonNegativeFloat = 0
    room_vacancy_pct: NonNegativeFloat = 0
    management_fee_pct: NonNegativeFloat = 0
    leasing_fee_pct: NonNegativeFloat = 0

    @model_validator(mode="after")
    def _end_after_start(self) -> _RentalPlanBase:
        if self.start_month is not None and self.end_month is not None and self.end_month < self.start_month:
            raise ValueError("end_month must be greater than or equal to start_month")
        return self


class NotRentedRentalPlan(_RentalPlanBase):
    rental_mode: Literal[RentalMode.NOT_RENTED] = RentalMode.NOT_RENTED


class WholePropertyRentalPlan(_RentalPlanBase):
    rental_mode: Literal[RentalMode.RENT_WHOLE_PROPERTY] = RentalMode.RENT_WHOLE_PROPERTY
    monthly_rent_usd: NonNegativeFloat


class TransitionWholePropertyRentalPlan(_RentalPlanBase):
    rental_mode: Literal[RentalMode.TRANSITION_TO_WHOLE_PROPERTY_RENTAL] = (
        RentalMode.TRANSITION_TO_WHOLE_PROPERTY_RENTAL
    )
    monthly_rent_usd: NonNegativeFloat


class RoomRentalPlan(_RentalPlanBase):
    rental_mode: Literal[RentalMode.RENT_ROOMS_WHILE_OWNER_LIVES_THERE] = RentalMode.RENT_ROOMS_WHILE_OWNER_LIVES_THERE
    room_rent_monthly_usd: NonNegativeFloat


RentalPlan = Annotated[
    NotRentedRentalPlan | WholePropertyRentalPlan | TransitionWholePropertyRentalPlan | RoomRentalPlan,
    Field(discriminator="rental_mode"),
]


class PositionProvenance(ApiModel):
    source_id: str | None = None
    snapshot_id: str | None = None
    as_of: str | None = None


class AccountBalance(ApiModel):
    account_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_\-]*$")
    account_type: AccountType
    owner_actor_id: str
    balance_usd: float
    provenance: PositionProvenance = Field(default_factory=PositionProvenance)


class _AssetPositionBase(ApiModel):
    asset_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_\-]*$")
    owner_actor_id: str
    value_usd: float
    provenance: PositionProvenance = Field(default_factory=PositionProvenance)


class GenericSp500StockPosition(_AssetPositionBase):
    asset_type: Literal[AssetType.GENERIC_SP500_STOCK] = AssetType.GENERIC_SP500_STOCK
    cost_basis_usd: float | None = None


class CryptoAssetPosition(_AssetPositionBase):
    """A crypto holding modeled as a single fungible quantity (e.g. BTC, ETH).

    Crypto value moves with `MarketBundle.crypto_value_multipliers` (currently a
    placeholder array of ones — fitted crypto models are deferred). Realized gain on
    sale is treated as ordinary income (federal + California) until a richer
    short/long-term cap-gains model lands; the choice is documented near the funding
    chain rather than in the schema.
    """

    asset_type: Literal[AssetType.CRYPTO] = AssetType.CRYPTO
    asset_symbol: str = Field(description="Ticker symbol (BTC, ETH, etc.). Free-form; not validated against a catalog.")
    quantity: NonNegativeFloat | None = None
    cost_basis_usd: float | None = None
    source_account_id: str | None = None


class PrivateEquityPosition(ApiModel):
    """An opening private-equity position.

    Either `units` or `value_usd` must be supplied (often both):

    - When `value_usd` is set, it is the authoritative month-0 mark — e.g. a tender-offer
      or manual mark carried through from a `PortfolioStatement.PrivateEquityLot`.
    - When `value_usd` is absent, the month-0 mark is derived from
      `units × MarketBundleMetadata.current_private_equity_price_usd`. Callers without
      an independent mark (such as the browser UI, which stores units only) should leave
      `value_usd` unset; the simulator owns the derivation.
    """

    asset_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_\-]*$")
    owner_actor_id: str
    asset_type: Literal[AssetType.PRIVATE_EQUITY] = AssetType.PRIVATE_EQUITY
    units: NonNegativeFloat | None = None
    value_usd: NonNegativeFloat | None = None
    cost_basis_usd: float | None = None
    provenance: PositionProvenance = Field(default_factory=PositionProvenance)

    @model_validator(mode="after")
    def _require_units_or_value(self) -> PrivateEquityPosition:
        if self.units is None and self.value_usd is None:
            raise ValueError(
                f"PrivateEquityPosition {self.asset_id!r} must set units or value_usd "
                "(or both); the simulator needs one to derive the opening mark."
            )
        return self


AssetPosition = Annotated[
    GenericSp500StockPosition | CryptoAssetPosition | PrivateEquityPosition, Field(discriminator="asset_type")
]


class InitialBalanceSheet(ApiModel):
    accounts: tuple[AccountBalance, ...] = ()
    assets: tuple[AssetPosition, ...] = ()
    liabilities: tuple[Any, ...] = Field(default=(), max_length=0)


class MarketRequest(ApiModel):
    market_model_id: str = "current_market_model"
    rollout_count: PositiveInt = 128
    horizon_months: PositiveInt = 360
    seed: int


class Scenario(ApiModel):
    scenario_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_\-]*$")
    label: str
    enabled: bool = True
    color: str | None = None
    actors: tuple[Actor, ...] = Field(min_length=1)
    events: tuple[Event, ...] = ()
    policies: tuple[Policy, ...] = ()
    property_selection: PropertySelection = Field(default_factory=PropertySelection)
    financing: Financing = Field(default_factory=Financing)
    occupancy_plan: OccupancyPlan = Field(default_factory=OccupancyPlan)
    rental_plan: RentalPlan = Field(default_factory=NotRentedRentalPlan)
    tax_profile: TaxProfile = Field(default_factory=TaxProfile)
    transaction_costs: TransactionCosts = Field(default_factory=TransactionCosts)
    property_assumptions: PropertyAssumptions = Field(default_factory=PropertyAssumptions)
    initial_balance_sheet: InitialBalanceSheet = Field(default_factory=InitialBalanceSheet)
    tax_regimes: tuple[TaxRegime, ...] = ()

    @property
    def location_id(self) -> str | None:
        return self.property_selection.location_id


class ScenarioSet(ApiModel):
    scenario_set_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_\-]*$")
    title: str
    market_request: MarketRequest
    report_spec: ReportSpec = Field(default_factory=ReportSpec)
    scenarios: tuple[Scenario, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _scenario_ids_are_unique(self) -> ScenarioSet:
        scenario_ids = [scenario.scenario_id for scenario in self.scenarios]
        duplicate_ids = sorted({scenario_id for scenario_id in scenario_ids if scenario_ids.count(scenario_id) > 1})
        if duplicate_ids:
            raise ValueError(f"scenario ids must be unique: {duplicate_ids}")
        return self


class ScenarioAcceptedSummary(ApiModel):
    enabled: bool
    property_id: PropertyId | None = None
    location_id: str | None = None


class ExogenousPathIdentity(ApiModel):
    rollout_index: NonNegativeInt
    path_set_id: str
    exogenous_path_id: str
    market_model_id: str
    market_model_version_id: str = "unknown"
    scenario_generator_id: str = "market_bundle_provider"
    scenario_generator_version_id: str = "unknown"
    evidence_set_id: str = "unknown"
    calibration_artifact_id: str = "unknown"
    risk_factor_set_id: str = "core_market_factors:v1"
    seed: int
    event_stream_ids: tuple[str, ...] = ()


class ProjectionTrajectoryIdentity(ApiModel):
    scenario_id: str
    rollout_index: NonNegativeInt
    path_set_id: str
    exogenous_path_id: str
    scenario_input_id: str
    policy_program_set_id: str
    projection_trajectory_id: str


class RolloutStatus(ApiModel):
    rollout_index: NonNegativeInt
    status: RolloutStatusType
    min_cash_usd: float
    first_negative_cash_month_index: NonNegativeInt | None = None
    first_failed_obligation_month_index: NonNegativeInt | None = None
    failed_obligation_count: NonNegativeInt = 0
    unpaid_obligation_usd: NonNegativeFloat = 0.0


class RolloutStatusSummary(ApiModel):
    total_rollout_count: NonNegativeInt = 0
    counts_by_status: dict[RolloutStatusType, NonNegativeInt] = Field(default_factory=dict)

    @classmethod
    def from_statuses(cls, statuses: tuple[RolloutStatus, ...]) -> RolloutStatusSummary:
        status_counts = Counter(status.status for status in statuses)
        counts = (
            {
                status: status_counts[status]
                for status in RolloutStatusType
                if status is not RolloutStatusType.FAILED or status_counts[status] > 0
            }
            if statuses
            else {}
        )
        return cls(total_rollout_count=len(statuses), counts_by_status=counts)


class ScenarioResult(ApiModel):
    scenario_id: str
    scenario_label: str
    summary: ScenarioAcceptedSummary
    projection_trajectories: tuple[ProjectionTrajectoryIdentity, ...] = ()
    rollout_statuses: tuple[RolloutStatus, ...] = ()
    metric_fan_columns: dict[str, ColumnarTable] = Field(default_factory=dict)
    monthly_columns: ColumnarTable | None = None
    terminal_columns: ColumnarTable | None = None
    actions: tuple[Action, ...] = ()
    policy_decisions: tuple[PolicyDecision, ...] = ()
    market_observations: tuple[MarketObservation, ...] = ()
    chart_accounts: tuple[ChartAccount, ...] = ()
    journal_entries: tuple[JournalEntry, ...] = ()
    postings: tuple[Posting, ...] = ()
    balance_snapshots: tuple[BalanceSnapshot, ...] = ()
    tax_lots: tuple[TaxLot, ...] = ()
    lot_dispositions: tuple[LotDisposition, ...] = ()
    liabilities: tuple[LiabilityState, ...] = ()
    accounting_details: tuple[AccountingDetail, ...] = ()
    obligations: tuple[Obligation, ...] = ()
    funding_decisions: tuple[FundingDecision, ...] = ()
    settlement_results: tuple[SettlementResult, ...] = ()
    failure_events: tuple[FailureEvent, ...] = ()
    warnings: tuple[str, ...] = ()


class ScenarioSetRunResponse(ApiModel):
    scenario_set_id: str
    request: ScenarioSet
    market_request: MarketRequest
    report_spec: ReportSpec
    market_metadata: dict[str, Any] | None = None
    projection_run: ProjectionRun | None = None
    exogenous_paths: tuple[ExogenousPathIdentity, ...] = ()
    scenario_results: tuple[ScenarioResult, ...]
    warnings: tuple[str, ...] = ()
