"""Plaid Spend API response models."""

from __future__ import annotations

from datetime import date as date_type, datetime as datetime_type
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from finance.plaid.spend.allowance import (
    AllowanceView,
    AnalysisCategory,
    Disposition,
    Kind,
    Period,
    PeriodId,
    PlaidCounterparty,
    ReversalPair,
    Rule,
    TransactionPeriodId,
)


class AlertState(StrEnum):
    NORMAL = "normal"
    WARNING = "warning"
    EXCEEDED = "exceeded"
    UNAVAILABLE = "unavailable"


class StatementReason(StrEnum):
    COUNTED = "counted"
    OUTSIDE_CYCLE = "outside_cycle"
    SUPERSEDED_PENDING = "superseded_pending"
    CARD_PAYMENT = "card_payment"
    OTHER_CURRENCY = "other_currency"
    UNAVAILABLE = "unavailable"


class CardConfigurationView(BaseModel):
    """Safe web projection of a configured card, without its Plaid account ID."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str
    enabled: bool
    limit_minor_units: int | None
    alert_threshold_percent: int | None


class AllowanceConfigurationView(BaseModel):
    """Safe web projection of the flexible allowance policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    monthly_minor_units: int
    activation_at: date_type
    currency: str
    spending_account_count: int
    max_sync_age_hours: int
    forecast_basis_period_id: PeriodId
    rules: list[Rule]
    reversal_pairs: list[ReversalPair] = Field(default_factory=list)
    analysis_categories: dict[str, AnalysisCategory]


class SpendConfigurationView(BaseModel):
    """Configuration currently loaded by the service, omitting all account IDs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    time_zone: str
    cards: list[CardConfigurationView]
    allowance: AllowanceConfigurationView | None


class StatementCycle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["statement"] = "statement"
    start: date_type
    through: date_type


class ProvisionalCardPeriod(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["provisional"] = "provisional"
    start: date_type
    through: date_type


class UnavailableCardPeriod(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["unavailable"] = "unavailable"


type CardPeriod = StatementCycle | ProvisionalCardPeriod | UnavailableCardPeriod


class CardView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    account_id: str
    label: str
    account_name: str
    institution_name: str | None
    mask: str | None
    currency: str
    statement_period: Annotated[CardPeriod, Field(discriminator="kind")]
    spend_minor_units: int | None
    posted_minor_units: int | None
    pending_minor_units: int | None
    limit_minor_units: int | None
    alert_threshold_percent: int | None
    spend_percent: Decimal | None
    alert_state: AlertState
    last_synced_at: datetime_type | None


class SpendView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    generated_at: datetime_type
    cards: list[CardView]
    allowance: AllowanceView | None = None
    dashboard_url: str | None = None


class PlaidTransactionLocation(BaseModel):
    """Plaid's location fields for a transaction at a physical location."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    address: str | None = None
    city: str | None = None
    region: str | None = None
    postal_code: str | None = None
    country: str | None = None
    lat: float | None = None
    lon: float | None = None
    store_number: str | None = None


class PlaidPaymentMeta(BaseModel):
    """Plaid's inter-bank transfer metadata."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    reference_number: str | None = None
    ppd_id: str | None = None
    payee: str | None = None
    by_order_of: str | None = None
    payer: str | None = None
    payment_method: str | None = None
    payment_processor: str | None = None
    reason: str | None = None


class PlaidPersonalFinanceCategory(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    primary: str | None = None
    detailed: str | None = None
    confidence_level: str | None = None
    version: str | None = None


class PlaidBusinessFinanceCategory(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    primary: str | None = None
    detailed: str | None = None
    confidence_level: str | None = None


class PlaidClientCustomization(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    custom_entity_id: str | None = None


class PlaidTransactionDetails(BaseModel):
    """Named Plaid Transaction fields not already in the compact row.

    Source: https://github.com/plaid/plaid-openapi/blob/master/2020-09-14.yml
    Optional defaults accommodate historical records and fields an institution omitted.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    account_id: str | None = None
    transaction_id: str | None = None
    pending_transaction_id: str | None = None
    amount: Decimal | None = Field(default=None, description="Original Plaid amount in major currency units.")
    iso_currency_code: str | None = None
    unofficial_currency_code: str | None = None
    account_owner: str | None = None
    check_number: str | None = None
    category: list[str] | None = None
    category_id: str | None = None
    original_description: str | None = None
    authorized_date: date_type | None = None
    authorized_datetime: datetime_type | None = None
    datetime: datetime_type | None = None
    payment_channel: str | None = None
    payment_meta: PlaidPaymentMeta | None = None
    location: PlaidTransactionLocation | None = None
    personal_finance_category: PlaidPersonalFinanceCategory | None = None
    business_finance_category: PlaidBusinessFinanceCategory | None = None
    transaction_type: str | None = None
    transaction_code: str | None = None
    logo_url: str | None = None
    website: str | None = None
    merchant_entity_id: str | None = None
    personal_finance_category_icon_url: str | None = None
    running_balance: Decimal | None = Field(default=None, description="Plaid-reported balance in major currency units.")
    client_customization: PlaidClientCustomization | None = None


class PaceEffect(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    period_id: PeriodId
    amount_minor_units: int


class AnalysisCategoryView(BaseModel):
    """Resolved category metadata for a transaction, including its stable key."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    label: str
    color: str


class SpendTransactionRow(BaseModel):
    """A read-only explanation plus named, typed Plaid source fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    date: date_type = Field(description="Purchase day selected in the policy time zone.")
    plaid_date: date_type | None = Field(
        default=None, description="Original Plaid date used by statement and date-range rules."
    )
    account_label: str
    name: str
    merchant_name: str | None
    amount_minor_units: int
    currency: str
    pending: bool
    allowance_in_scope: bool
    disposition: Disposition | None
    rule_number: int | None = Field(
        default=None, description="1-based position in the ordered rule list; the first match governs."
    )
    rule: Rule | None = None
    reversal_pair: ReversalPair | None = None
    effective_kind: Kind | None = Field(
        default=None,
        description="Resolved accounting kind: excluded for a confirmed reversal, else the rule's kind or null.",
    )
    allowance_minor_units: int
    pace_effects: list[PaceEffect]
    statement_minor_units: int | None
    statement_reason: StatementReason | None
    pfc_primary: str | None
    pfc_detailed: str | None
    merchant_category_code: str | None
    category: AnalysisCategoryView | None = None
    counterparties: list[PlaidCounterparty] = Field(default_factory=list)
    details: PlaidTransactionDetails = Field(default_factory=PlaidTransactionDetails)


class ExcludedFlowSummary(BaseModel):
    """Policy-excluded ledger flows, gross in each direction and never netted."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    category: AnalysisCategoryView
    currency: str
    debit_minor_units: int = Field(ge=0)
    credit_minor_units: int = Field(ge=0)
    transaction_count: int = Field(ge=0)
    pending_count: int = Field(ge=0)


class TransactionPeriodSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    transaction_count: int
    net_allowance_spend_minor_units: int
    unmatched_charge_count: int
    unmatched_charge_minor_units: int
    excluded_flows: list[ExcludedFlowSummary] = Field(default_factory=list)


class SpendTransactionsView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    generated_at: datetime_type
    requested_period_id: TransactionPeriodId
    period: Period
    summary: TransactionPeriodSummary
    allowance: AllowanceView | None
    rows: list[SpendTransactionRow]
