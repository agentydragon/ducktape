from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, NonNegativeFloat, model_validator

# ---------------------------------------------------------------------------
# Base configurations.
# ---------------------------------------------------------------------------
#
# Shared simulator models use ordinary snake_case field names. App-specific
# HTTP boundaries may adapt those names for browser compatibility, but that
# conversion is not a core schema concern.


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class InternalModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LenientSourceModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


Percentage = Annotated[NonNegativeFloat, Field(le=100)]


# ---------------------------------------------------------------------------
# Request shapes (boundary with the browser).
# ---------------------------------------------------------------------------


class PropertyRequest(InternalModel):
    id: str
    price_usd: float
    beds: float
    hoa_monthly_usd: float = 0
    rent_zestimate_usd: float | None = None
    tax_rate_override: float | None = None


class KnobsConfig(ApiModel):
    down_payment_pct: float
    credit_score: float
    custom_mortgage_rate: float
    custom_mortgage_term_years: float
    starting_portfolio_usd: float
    hold_years: float
    appreciation_rate: float
    sp500_rate: float
    maintenance_pct: float
    owner_occupancy_years: float
    inflation: float
    vacancy_pct: float
    mgmt_pct: float
    leasing_fee_pct: float
    rooms_rented_while_living: float
    room_rent_monthly_usd: float
    room_vacancy_pct: float
    portfolio_liquidation_tax_pct: float
    insurance_annual_usd: float
    closing_cost_buy_pct: float
    closing_cost_sell_pct: float
    depreciable_basis_pct: float
    financing_mode: Literal["cash", "fixed_30", "fixed_15", "custom"]
    occupancy_type: Literal["primary_residence", "second_home", "investment"]


class ScenarioKnobs(KnobsConfig):
    """`KnobsConfig` augmented with per-rollout path overrides.

    `simulate_arrangement` reads these (when present) to drive each scenario
    along the rollout's drawn macro path; absent / `None` fields fall back to
    `KnobsConfig`'s deterministic single-rate growth. Analysis-only callers
    pass a `ScenarioKnobs` with every override unset (use
    `ScenarioKnobs.from_knobs(knobs)`)."""

    home_value_multipliers: list[float] | None = None
    sale_home_value_multipliers: list[float] | None = None
    portfolio_multipliers: list[float] | None = None
    rent_multipliers: list[float] | None = None
    expense_inflation_multipliers: list[float] | None = None

    @classmethod
    def from_knobs(cls, knobs: KnobsConfig) -> ScenarioKnobs:
        return cls.model_validate(knobs.model_dump())


# Financing + amortization (simulation outputs).
# ---------------------------------------------------------------------------


class Financing(InternalModel):
    financing_mode: str
    financing_label: str
    occupancy_type: str
    occupancy_label: str
    credit_score: float
    down_payment_pct: float
    loan_to_value_pct: float
    term_years: float
    rate_pct: float
    base_rate_pct: float | None
    credit_spread_pct: float | None
    occupancy_spread_pct: float | None
    ltv_spread_pct: float | None
    is_custom: bool
    is_cash: bool


class AmortizationMonth(InternalModel):
    month_index: int
    payment_usd: float
    interest_usd: float
    principal_usd: float
    balance_usd: float
    cumulative_interest_usd: float
    cumulative_principal_usd: float


class AmortizationYear(InternalModel):
    year: int
    balance_usd: float
    cum_interest_usd: float
    cum_principal_usd: float
    year_interest_usd: float
    year_principal_usd: float


class AmortizationSchedule(InternalModel):
    payment_usd: float
    monthly: list[AmortizationMonth]
    yearly: list[AmortizationYear]


# ---------------------------------------------------------------------------
# House simulation (per-property, per-knobs run).
# ---------------------------------------------------------------------------


LedgerActor = str
LedgerDomain = Literal["cash", "equity"]


class LedgerRow(InternalModel):
    month_index: int
    year_index: int
    actor: LedgerActor
    domain: LedgerDomain
    category: str
    amount_usd: float


class MonthRow(InternalModel):
    month_index: int
    year_index: int
    phase: Literal["occupied", "rental"]
    home_value_usd: float
    mortgage_balance_usd: float
    mortgage_interest_usd: float
    mortgage_principal_usd: float
    property_tax_usd: float
    insurance_usd: float
    hoa_usd: float
    maintenance_usd: float
    tenant_rent_usd: float
    rooms_rented: int
    room_rent_usd: float
    tax_shield_usd: float
    active_rental_share: float
    monthly_depreciation_usd: float
    cumulative_depreciation_usd: float
    suspended_passive_losses_usd: float
    rental_taxable_income_usd: float
    passive_loss_offset_used_usd: float
    rental_income_tax_usd: float
    owner_equity_ledger_usd: float


class SaleOutcome(InternalModel):
    selling_costs_usd: float
    gross_equity_usd: float
    adjusted_basis_usd: float
    total_gain_usd: float
    capital_gain_usd: float
    recapture_gain_usd: float
    exclusion_usd: float
    taxable_gain_usd: float
    recapture_tax_usd: float
    capital_gains_tax_usd: float
    passive_loss_release_benefit_usd: float
    suspended_passive_losses_usd: float
    cg_tax_usd: float
    net_sale_proceeds_usd: float


class Terminal(InternalModel):
    final_month: MonthRow
    final_home_value_usd: float
    final_loan_balance_usd: float
    owner_equity_ledger_usd: float
    sale: SaleOutcome
    owner_net_proceeds_usd: float


class Result(InternalModel):
    # Property + scenario knobs the simulator was driven with. Carrying the
    # typed models here lets `analysis.py` (project_summary, project_yearly_ledger,
    # …) read fields without re-deriving request state.
    property: PropertyRequest
    knobs: ScenarioKnobs
    purchase_price_usd: float
    down_payment_usd: float
    closing_buy_usd: float
    portfolio_liquidation_tax_usd: float
    initial_outlay_usd: float
    loan_amount_usd: float
    financing: Financing
    tax_rate: float
    initial_annual_tax_usd: float
    hold_months: int
    occupied_months: int
    depreciable_basis_usd: float
    amortization: AmortizationSchedule
    months: list[MonthRow]
    ledger: list[LedgerRow]
    validations: list[str]
    terminal: Terminal


# ---------------------------------------------------------------------------
# Projections / sale path (consumed by browser charts).
# ---------------------------------------------------------------------------


class MonthlySalePathRow(InternalModel):
    month_index: int
    buy_liquid_usd: float
    buy_locked_equity_usd: float
    buy_path_usd: float
    project_buy_liquid_usd: float
    project_own_usd: float
    net_sale_proceeds_usd: float
    gross_equity_usd: float
    owner_sale_claim_usd: float
    owner_equity_ledger_usd: float


# ---------------------------------------------------------------------------
# Columnar response tables.
# ---------------------------------------------------------------------------


class ColumnarTable(InternalModel):
    """Rectangular, JSON-safe table payload.

    Each entry in `columns` is one complete column with `row_count` values.
    This is the HTTP shape for array-like simulator outputs; UI libraries that
    still need row objects can transpose it at the frontend boundary.
    """

    row_count: int
    columns: dict[str, list[Any]]

    @model_validator(mode="after")
    def _columns_match_row_count(self) -> ColumnarTable:
        if self.row_count < 0:
            raise ValueError("row_count must be non-negative")
        lengths = {name: len(values) for name, values in self.columns.items()}
        mismatched = {name: length for name, length in lengths.items() if length != self.row_count}
        if mismatched:
            raise ValueError(f"column lengths must equal row_count={self.row_count}: {mismatched}")
        return self


# ---------------------------------------------------------------------------
# Lenient source fetch shapes used by evidence adapters.
# ---------------------------------------------------------------------------


class ManifoldAnswer(LenientSourceModel):
    text: str | None = None
    probability: float | None = None


class ManifoldRawMarket(LenientSourceModel):
    id: str
    question: str | None = None
    outcome_type: str | None = None
    probability: float | None = None
    volume: float | None = None
    volume_24_hours: float | None = None
    total_liquidity: float | None = None
    unique_bettor_count: int | None = None
    last_updated_time: int | None = None
    last_bet_time: int | None = None
    close_time: int | None = None
    is_resolved: bool | None = None
    resolution: str | None = None
    creator_username: str | None = None
    creator_name: str | None = None
    slug: str | None = None
    answers: list[ManifoldAnswer] = Field(default_factory=list)


class ManifoldMarketSnapshot(StrictModel):
    id: str
    question: str | None = None
    outcome_type: str | None = None
    probability: float | None = None
    volume: float | None = None
    volume_24_hours: float | None = None
    total_liquidity: float | None = None
    unique_bettor_count: int | None = None
    last_updated_time: int | None = None
    last_bet_time: int | None = None
    close_time: int | None = None
    is_resolved: bool | None = None
    resolution: str | None = None
    url: str
    answers: list[ManifoldAnswer] = Field(default_factory=list)
