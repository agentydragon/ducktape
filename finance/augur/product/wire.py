"""Product-language projection request and response wire types."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    Field,
    NonNegativeFloat,
    NonNegativeInt,
    PositiveFloat,
    PositiveInt,
    StringConstraints,
    model_validator,
)

from finance.augur.api.schemas import (
    ApiModel,
    BasisPointPercentage,
    Frame,
    NonNegativeCurrencyAmount,
    Percentage,
    PositiveCurrencyAmount,
)
from finance.augur.model.series import SecuritySymbol
from finance.augur.product.asset_key import AssetKey
from finance.augur.sim.fixed_point import validate_currency_quantum


class SpendIndex(StrEnum):
    NONE = "none"
    INFLATION = "inflation"


PrivateEquityEventKind = Literal[
    "tender",
    "admin_mark_update",
    "public_market_open",
    "acquisition_cashout",
    "legal_impairment",
    "forced_recovery",
    "collapse",
]
PrivateEquityRegime = Literal["private_operating", "public_market", "acquired", "collapsed"]
PrivateEquityOpportunityOutcome = Literal[
    "sold", "floor_satisfied", "capacity_zero", "liquidity_blocked", "no_policy", "no_units", "nonpositive_mark"
]
MetricName = Literal[
    "cash",
    "holding_value",
    "private_equity_value",
    "property_value",
    "mortgage_balance",
    "home_equity",
    "liquid_net_worth",
    "net_worth",
    "shortfall",
    "bond_value",
]
# Decimal integer strings keep Int64 money exact across the JSON/JavaScript boundary.
type CurrencyQuanta = Annotated[str, StringConstraints(pattern=r"^-?(0|[1-9][0-9]*)$")]
MAX_HORIZON_MONTHS = 100 * 12


class SleeveWeight(ApiModel):
    """One holding's share of the target allocation, as an integer relative weight.

    Only RATIOS matter, so `(3, 1)` and `(30, 10)` are the same target. Weight 0 means the
    holding is OUTSIDE the target: never sold to fund the band, and not counted when measuring
    what is overweight. That is how a position you intend to keep — private equity before
    liquidity, a bond held to maturity — is expressed, and it is why zero is allowed here while
    the sim's `SleeveTarget` requires a positive weight: this is the UI's way of saying "not in
    the target", and the lowering drops it rather than passing a meaningless zero down.
    """

    symbol: SecuritySymbol
    weight: NonNegativeInt


class FundingPolicy(ApiModel):
    """How the owner funds spending: hold cash in a band, sell toward a target when it runs low.

    Replaces the old trigger/sale-amount buffer with an (s,S) band, and the ordered sell list
    with a target the sales move TOWARD. Crossing the floor refills to the CEILING, not back to
    the floor — refilling to the floor would put the owner back at the trigger next month,
    making them a forced seller into every dip.

    The ceiling is the refill TARGET, not an invest-above-this rule: surplus cash above it
    accumulates and nothing buys with it.
    """

    cash_floor: NonNegativeCurrencyAmount = Decimal(0)
    cash_ceiling: NonNegativeCurrencyAmount = Decimal(0)
    # Matches PrivateEquityTenderPolicyWire.index_floor_to_inflation: when true, both bounds are
    # today-dollar real-terms targets inflated by CPI each month. When false they stay nominal.
    # Both bounds share the flag because indexing them differently would let a band that starts
    # valid invert partway through the horizon, and an inverted band has no interior.
    cash_band_index_to_inflation: bool = True
    sleeve_weights: tuple[SleeveWeight, ...] = Field(
        default=(),
        description=(
            "Target weight per holding symbol. Empty disables auto-sale entirely — the owner "
            "never sells to fund the band, and an unaffordable obligation is ruin. There is no "
            "'derive it for me' sentinel: the caller has each holding's current value and seeds "
            "the weights from it, which is what makes the default 'hold what you have'. Private "
            "equity can never appear — it has no symbol, and is sold only at tender events."
        ),
    )

    @model_validator(mode="after")
    def _reject_inverted_band_and_duplicate_symbols(self) -> FundingPolicy:
        if self.cash_floor > self.cash_ceiling:
            raise ValueError(
                f"cash floor must not exceed its ceiling; got floor={self.cash_floor}, "
                f"ceiling={self.cash_ceiling}. An inverted band has no interior, so every "
                "balance crosses both bounds at once."
            )
        symbols = [sleeve.symbol for sleeve in self.sleeve_weights]
        if len(set(symbols)) != len(symbols):
            duplicated = sorted({s for s in symbols if symbols.count(s) > 1})
            raise ValueError(
                f"sleeve weights name {duplicated} more than once; a holding weighted twice is double-counted"
            )
        return self


class PrivateEquityTenderPolicyWire(ApiModel):
    """User-facing PE tender policy. At each tender event for any held PE position, the
    engine sells units to lift liquid net worth (cash + non-PE holdings) to this floor.

    `liquid_net_worth_floor` of 0 disables PE sales entirely (LNW always >= floor).
    `index_floor_to_inflation` (default true) inflates the floor with CPI so the real-terms
    target stays constant over long horizons. Set to false to keep the floor nominal.
    """

    liquid_net_worth_floor: NonNegativeCurrencyAmount = Decimal(0)
    index_floor_to_inflation: bool = True


class CashFinancing(ApiModel):
    kind: Literal["cash"] = "cash"


class MortgageFinancing(ApiModel):
    kind: Literal["mortgage"] = "mortgage"
    term_months: Literal[180, 360]
    down_payment_pct: NonNegativeFloat
    annual_rate_pct: NonNegativeFloat


type PropertyFinancing = Annotated[CashFinancing | MortgageFinancing, Field(discriminator="kind")]


class RentalIncomePlan(ApiModel):
    """The property is being rented out to a tenant.

    `fraction_rented` = 1.0 means the whole property is rented (pure investment or
    user lives elsewhere). `fraction_rented` < 1.0 means partial rental (e.g. owner
    occupies the main unit and rents the ADU / rents rooms).

    `full_property_monthly_rent` is the full-property market rent before vacancy and
    management fees. Collected rent is this amount multiplied by `fraction_rented` and
    `(1 - vacancy_pct)`. If `None`, the translator falls back to `Property.rent_estimate`
    for the purchased property; if that's also missing, the request is rejected.
    """

    full_property_monthly_rent: NonNegativeCurrencyAmount | None = None
    fraction_rented: PositiveFloat = Field(default=1.0, le=1.0)
    # 0..1 multiplier on collected rent. Captures marketing-time vacancy + tenant turnover
    # vacancy in a smoothed-average form; per-rollout stochastic vacancy is a future model.
    vacancy_pct: NonNegativeFloat = Field(default=0.05, le=1.0)


class RentalManagement(ApiModel):
    """Property management agency terms.

    Management fee fires monthly against collected (post-vacancy) rent.
    Leasing fee fires every `avg_tenancy_months` while the property is rented (first fire
    when the rental status activates). Captures lifetime tenant-placement cost without
    modeling specific tenants.
    """

    management_fee_pct: NonNegativeFloat = Field(default=8.0, le=100.0)
    leasing_fee_months: NonNegativeFloat = Field(default=1.0)
    avg_tenancy_months: PositiveInt = 24


class SetRentedFractionEventWire(ApiModel):
    """Lifecycle event: set a property's rented_fraction to a new value at `month`.

    Subsumes start/stop/change-rental-plan — 1.0 is "full rental", 0.0 is "stop renting".
    """

    kind: Literal["set_rented_fraction"] = "set_rented_fraction"
    month: PositiveInt
    rented_fraction: NonNegativeFloat = Field(le=1.0)


class SetPrimaryResidenceEventWire(ApiModel):
    """Lifecycle event: make the purchased property the owner's primary residence, or clear it."""

    kind: Literal["set_primary_residence"] = "set_primary_residence"
    month: PositiveInt
    is_primary_residence: bool


class CapitalImprovementEventWire(ApiModel):
    """Lifecycle event: cash debit + building basis bump (e.g. new roof, kitchen remodel)."""

    kind: Literal["capital_improvement"] = "capital_improvement"
    month: PositiveInt
    amount: PositiveCurrencyAmount
    description: str = ""


class PropertySaleEventWire(ApiModel):
    """Lifecycle event: property is sold at `month`. Mortgage paid off; gain/recapture taxed."""

    kind: Literal["property_sale"] = "property_sale"
    month: PositiveInt
    # Basis points, not percent, is the resolution the simulator carries this at.
    closing_cost_pct: BasisPointPercentage


type PropertyLifecycleEventWire = Annotated[
    SetRentedFractionEventWire | SetPrimaryResidenceEventWire | CapitalImprovementEventWire | PropertySaleEventWire,
    Field(discriminator="kind"),
]


class PropertyPurchase(ApiModel):
    property_id: str
    closing_cost_pct: NonNegativeFloat = 1.5
    financing: PropertyFinancing
    # Owner-occupied: gates the federal/CA mortgage interest deduction (§163(h)(3)). When false,
    # the property is treated as an investment / second home and no MID policy is built. No
    # default: callers must commit to an answer rather than inherit one silently.
    is_primary_residence: bool
    # The property is rented (whole or partial) from month 0. Mid-horizon transitions live
    # in `lifecycle_events`.
    initial_rental: RentalIncomePlan | None = None
    # Property is managed by an agency. Requires `initial_rental` set.
    rental_management: RentalManagement | None = None
    # Mid-horizon transitions (start/stop renting, change rental plan, capital improvements)
    # for this property.
    lifecycle_events: tuple[PropertyLifecycleEventWire, ...] = ()

    @model_validator(mode="after")
    def _rental_management_requires_rental(self) -> PropertyPurchase:
        if self.rental_management is not None and self.initial_rental is None:
            raise ValueError("rental_management requires initial_rental to be set")
        # Pure investment property must not also claim primary-residence MID treatment.
        if self.initial_rental is not None and self.initial_rental.fraction_rented >= 1.0 and self.is_primary_residence:
            raise ValueError("is_primary_residence must be False when fraction_rented == 1.0")
        # Lifecycle events past the sale are meaningless — the property is frozen on sale.
        # Reject any event at or after the first sale's month (a sale event itself is its own
        # endpoint, so we compare strictly).
        sale_month: int | None = None
        for event in self.lifecycle_events:
            if isinstance(event, PropertySaleEventWire) and (sale_month is None or event.month < sale_month):
                sale_month = event.month
        if sale_month is not None:
            for event in self.lifecycle_events:
                if event.month > sale_month or (
                    event.month == sale_month and not isinstance(event, PropertySaleEventWire)
                ):
                    raise ValueError(
                        f"lifecycle event at month {event.month} fires after sale at month {sale_month}; "
                        f"the property is frozen after sale"
                    )
        return self


DEFAULT_ANNUAL_INSURANCE_PCT = 0.4
DEFAULT_ANNUAL_MAINTENANCE_PCT = 1.0


class ScenarioKey(ApiModel):
    model_id: str
    currency_code: str = "USD"
    currency_quantum: Decimal = Decimal("0.01")
    horizon_months: PositiveInt = Field(le=MAX_HORIZON_MONTHS)
    monthly_spend: PositiveCurrencyAmount
    spend_index: SpendIndex
    funding_policy: FundingPolicy = Field(default_factory=FundingPolicy)
    pe_tender_policy: PrivateEquityTenderPolicyWire = Field(default_factory=PrivateEquityTenderPolicyWire)
    monthly_rent: NonNegativeCurrencyAmount = Decimal(0)
    rental_location_id: str | None = None
    property_purchase: PropertyPurchase | None = None
    annual_insurance_pct: NonNegativeFloat = DEFAULT_ANNUAL_INSURANCE_PCT
    annual_maintenance_pct: NonNegativeFloat = DEFAULT_ANNUAL_MAINTENANCE_PCT

    @classmethod
    def _normalize_currency_code(cls, code: str) -> str:
        normalized = code.strip().upper()
        if not normalized:
            raise ValueError("currency_code must not be empty")
        return normalized

    @classmethod
    def _validate_currency_quantum(cls, quantum: object) -> Decimal:
        return validate_currency_quantum(quantum)

    @model_validator(mode="before")
    @classmethod
    def _validate_currency(cls, value: object) -> object:
        if isinstance(value, dict):
            copy = dict(value)
            copy["currency_code"] = cls._normalize_currency_code(str(copy.get("currency_code", "USD")))
            copy["currency_quantum"] = cls._validate_currency_quantum(copy.get("currency_quantum", "0.01"))
            return copy
        return value

    @model_validator(mode="after")
    def _rent_location_consistency(self) -> ScenarioKey:
        if self.monthly_rent > 0 and self.rental_location_id is None:
            raise ValueError("rental_location_id is required when monthly_rent > 0")
        if self.monthly_rent == 0 and self.rental_location_id is not None:
            raise ValueError("rental_location_id must be unset when monthly_rent == 0")
        return self


class ProjectionSamplingRequest(ApiModel):
    scenario: ScenarioKey
    first_seed: NonNegativeInt
    rollout_count: PositiveInt
    metric: MetricName
    percentiles: tuple[Percentage, ...] = Field(min_length=1)

    @property
    def rollout_seeds(self) -> tuple[int, ...]:
        return tuple(range(int(self.first_seed), int(self.first_seed) + int(self.rollout_count)))


class RolloutRequest(ApiModel):
    scenario: ScenarioKey
    seed: NonNegativeInt


class ProductProjectionRequest(ApiModel):
    """Request both product projections for one shared scenario and seed batch."""

    scenario: ScenarioKey
    first_seed: NonNegativeInt
    rollout_count: PositiveInt
    metric: MetricName
    fan_percentiles: tuple[Percentage, ...] = Field(min_length=1)
    terminal_percentiles: tuple[Percentage, ...] = Field(min_length=1)

    @property
    def rollout_seeds(self) -> tuple[int, ...]:
        return tuple(range(int(self.first_seed), int(self.first_seed) + int(self.rollout_count)))


class TerminalMetrics(ApiModel):
    cash_quanta: CurrencyQuanta
    holding_value_quanta: CurrencyQuanta
    private_equity_value_quanta: CurrencyQuanta
    property_value_quanta: CurrencyQuanta
    mortgage_balance_quanta: CurrencyQuanta
    home_equity_quanta: CurrencyQuanta
    liquid_net_worth_quanta: CurrencyQuanta
    net_worth_quanta: CurrencyQuanta
    shortfall_quanta: CurrencyQuanta
    # Par face still on the books. In `net_worth_quanta` but deliberately not in
    # `liquid_net_worth_quanta`: held to maturity, a bond is neither marked nor saleable.
    bond_value_quanta: CurrencyQuanta
    failed_month_index: NonNegativeInt | None = None


class _RolloutEventBase(ApiModel):
    month_index: NonNegativeInt
    amount_quanta: CurrencyQuanta


class HoldingSaleEvent(_RolloutEventBase):
    kind: Literal["holding_sale"] = "holding_sale"
    asset: AssetKey
    asset_label: str | None = None
    units: NonNegativeFloat
    proceeds_quanta: CurrencyQuanta
    cost_basis_quanta: CurrencyQuanta


class PrivateEquityMarkerEvent(_RolloutEventBase):
    kind: Literal["private_equity_event"] = "private_equity_event"
    issuer_id: str
    asset: AssetKey
    asset_label: str | None = None
    event_kind: PrivateEquityEventKind
    regime: PrivateEquityRegime
    mark_quanta: CurrencyQuanta
    sale_capacity_fraction: NonNegativeFloat = Field(le=1.0)
    eligible_fraction: NonNegativeFloat = Field(le=1.0)
    forced_sale_fraction: NonNegativeFloat = Field(le=1.0)
    liquidity_blocked: bool
    forced_recovery_cashout_quanta: CurrencyQuanta


class PrivateEquityOpportunityEvent(_RolloutEventBase):
    kind: Literal["private_equity_opportunity"] = "private_equity_opportunity"
    issuer_id: str
    asset: AssetKey
    asset_label: str | None = None
    event_kind: PrivateEquityEventKind
    regime: PrivateEquityRegime
    outcome: PrivateEquityOpportunityOutcome
    mark_quanta: CurrencyQuanta
    sale_capacity_fraction: NonNegativeFloat = Field(le=1.0)
    eligible_fraction: NonNegativeFloat = Field(le=1.0)
    liquidity_blocked: bool
    floor_quanta: CurrencyQuanta
    # Liquid net worth at the tender opportunity; can go negative when spending/obligations
    # outrun liquid assets (the PE floor policy still evaluates against it).
    liquid_net_worth_quanta: CurrencyQuanta
    shortfall_quanta: CurrencyQuanta
    units_held: NonNegativeFloat
    sellable_units: NonNegativeFloat
    target_units: NonNegativeFloat
    proceeds_quanta: CurrencyQuanta


class MonthlyExpenseEvent(_RolloutEventBase):
    kind: Literal["monthly_expense"] = "monthly_expense"
    amount_due_quanta: CurrencyQuanta
    amount_paid_quanta: CurrencyQuanta
    shortfall_quanta: CurrencyQuanta


class OutsideRentPaymentEvent(_RolloutEventBase):
    kind: Literal["outside_rent"] = "outside_rent"
    amount_due_quanta: CurrencyQuanta
    amount_paid_quanta: CurrencyQuanta
    shortfall_quanta: CurrencyQuanta


class PropertyPurchaseEvent(_RolloutEventBase):
    kind: Literal["property_purchase"] = "property_purchase"
    property_id: str
    purchase_price_quanta: CurrencyQuanta
    down_payment_quanta: CurrencyQuanta
    mortgage_principal_quanta: CurrencyQuanta


class ClosingCostPaymentEvent(_RolloutEventBase):
    kind: Literal["closing_cost_payment"] = "closing_cost_payment"
    property_id: str


class MortgagePaymentEvent(_RolloutEventBase):
    kind: Literal["mortgage_payment"] = "mortgage_payment"
    interest_quanta: CurrencyQuanta
    principal_quanta: CurrencyQuanta


class PropertyTaxPaymentEvent(_RolloutEventBase):
    kind: Literal["property_tax_payment"] = "property_tax_payment"
    amount_due_quanta: CurrencyQuanta
    amount_paid_quanta: CurrencyQuanta
    shortfall_quanta: CurrencyQuanta


class HoaDuesPaymentEvent(_RolloutEventBase):
    kind: Literal["hoa_dues_payment"] = "hoa_dues_payment"
    amount_due_quanta: CurrencyQuanta
    amount_paid_quanta: CurrencyQuanta
    shortfall_quanta: CurrencyQuanta


class HomeownersInsurancePaymentEvent(_RolloutEventBase):
    kind: Literal["homeowners_insurance_payment"] = "homeowners_insurance_payment"
    amount_due_quanta: CurrencyQuanta
    amount_paid_quanta: CurrencyQuanta
    shortfall_quanta: CurrencyQuanta


class PropertyMaintenancePaymentEvent(_RolloutEventBase):
    kind: Literal["property_maintenance_payment"] = "property_maintenance_payment"
    amount_due_quanta: CurrencyQuanta
    amount_paid_quanta: CurrencyQuanta
    shortfall_quanta: CurrencyQuanta


class TaxAccrualEvent(_RolloutEventBase):
    kind: Literal["tax_accrual"] = "tax_accrual"
    jurisdiction_id: str
    tax_year_end_month: NonNegativeInt
    ordinary_income_quanta: CurrencyQuanta
    ltcg_quanta: CurrencyQuanta
    stcg_quanta: CurrencyQuanta
    ordinary_tax_quanta: CurrencyQuanta
    capital_gain_tax_quanta: CurrencyQuanta
    total_tax_quanta: CurrencyQuanta
    # MID under this jurisdiction's principal cap, 0.0 when not active.
    mortgage_interest_deduction_quanta: CurrencyQuanta
    # Sum of itemized lines (today MID is the only one). Consumer renders the larger of itemized
    # vs. standard as the "deduction used".
    itemized_deduction_quanta: CurrencyQuanta
    standard_deduction_quanta: CurrencyQuanta


class TaxPaymentEvent(_RolloutEventBase):
    kind: Literal["tax_payment"] = "tax_payment"
    obligation_type: str
    amount_due_quanta: CurrencyQuanta
    amount_paid_quanta: CurrencyQuanta
    shortfall_quanta: CurrencyQuanta


class RolloutFailureEvent(_RolloutEventBase):
    kind: Literal["failure"] = "failure"
    amount_due_quanta: CurrencyQuanta
    amount_paid_quanta: CurrencyQuanta
    shortfall_quanta: CurrencyQuanta


class SetRentedFractionMarkerEvent(_RolloutEventBase):
    """A `PropertyLifecycleEvent.SetRentedFraction` fired this month: a moment where the
    property's rented_fraction changed (start renting, stop renting, change %)."""

    kind: Literal["set_rented_fraction"] = "set_rented_fraction"
    property_id: str
    rented_fraction: float


class SetPrimaryResidenceMarkerEvent(_RolloutEventBase):
    """A primary-residence assignment event fired this month."""

    kind: Literal["set_primary_residence"] = "set_primary_residence"
    agent_id: str
    property_id: str | None
    is_primary_residence: bool


class CapitalImprovementMarkerEvent(_RolloutEventBase):
    """A `PropertyLifecycleEvent.CapitalImprovement` fired this month: cash debit and basis
    bump on the named property."""

    kind: Literal["capital_improvement"] = "capital_improvement"
    property_id: str


class PropertySaleMarkerEvent(_RolloutEventBase):
    """A `PropertyLifecycleEvent.Sale` fired this month: market sale of the named property
    with full closing-cost, recapture, §121 exclusion, and LTCG breakdown."""

    kind: Literal["property_sale"] = "property_sale"
    property_id: str
    gross_proceeds_quanta: CurrencyQuanta
    mortgage_payoff_quanta: CurrencyQuanta
    net_cash_to_owner_quanta: CurrencyQuanta
    realized_gain_quanta: CurrencyQuanta
    depreciation_recapture_quanta: CurrencyQuanta
    section_121_exclusion_quanta: CurrencyQuanta
    long_term_capital_gain_quanta: CurrencyQuanta


type RolloutEvent = Annotated[
    HoldingSaleEvent
    | PrivateEquityMarkerEvent
    | PrivateEquityOpportunityEvent
    | MonthlyExpenseEvent
    | OutsideRentPaymentEvent
    | PropertyPurchaseEvent
    | ClosingCostPaymentEvent
    | MortgagePaymentEvent
    | PropertyTaxPaymentEvent
    | HoaDuesPaymentEvent
    | HomeownersInsurancePaymentEvent
    | PropertyMaintenancePaymentEvent
    | TaxAccrualEvent
    | TaxPaymentEvent
    | RolloutFailureEvent
    | SetRentedFractionMarkerEvent
    | SetPrimaryResidenceMarkerEvent
    | CapitalImprovementMarkerEvent
    | PropertySaleMarkerEvent,
    Field(discriminator="kind"),
]

# The API preserves this order for same-month events. Frontend event metadata mirrors it so
# legends, marker stacks, and the event table stay in the same order as the rollout response.
ROLLOUT_EVENT_KIND_ORDER = (
    "property_purchase",
    "closing_cost_payment",
    "set_primary_residence",
    "set_rented_fraction",
    "capital_improvement",
    "property_sale",
    "private_equity_event",
    "private_equity_opportunity",
    "holding_sale",
    "tax_accrual",
    "tax_payment",
    "property_tax_payment",
    "hoa_dues_payment",
    "homeowners_insurance_payment",
    "property_maintenance_payment",
    "mortgage_payment",
    "monthly_expense",
    "outside_rent",
    "failure",
)


class RolloutOutput(ApiModel):
    seed: NonNegativeInt
    failed: bool
    monthly_metrics: Frame
    terminal_metrics: TerminalMetrics
    events: tuple[RolloutEvent, ...] = ()


class MetricFanResponse(ApiModel):
    model_id: str
    currency_code: str
    currency_quantum: str
    metric: MetricName
    monthly_metric_fan: Frame
    terminal_metric_percentiles: Frame
    failed_count: NonNegativeInt
    diagnostics: tuple[str, ...] = ()


class TerminalDistributionResponse(ApiModel):
    model_id: str
    currency_code: str
    currency_quantum: str
    metric: MetricName
    terminal_metric_percentiles: Frame
    terminal_metric_samples: Frame
    failed_count: NonNegativeInt
    diagnostics: tuple[str, ...] = ()


class ProductProjectionResponse(ApiModel):
    """Both product projection payloads, produced from one simulation batch."""

    metric_fan: MetricFanResponse
    terminal_distribution: TerminalDistributionResponse


class RolloutResponse(ApiModel):
    model_id: str
    currency_code: str
    currency_quantum: str
    rollout: RolloutOutput
    diagnostics: tuple[str, ...] = ()
