"""Validated Plaid spend configuration and API views."""

from __future__ import annotations

from datetime import date, datetime as datetime_type
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from finance.plaid.spend.allowance import AllowancePolicy, AllowanceView, Disposition, PlaidCounterparty, Rule


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


class CardConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    account_id: str = Field(min_length=1, max_length=255)
    label: str = Field(min_length=1, max_length=80)
    limit_minor_units: int | None = Field(default=None, gt=0)
    alert_threshold_percent: int | None = Field(default=None, ge=1, le=100)
    enabled: bool

    @field_validator("account_id", "label")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class SpendConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    cards: list[CardConfig]
    allowance: AllowancePolicy | None = None
    account_labels: dict[str, str] = Field(default_factory=dict)

    @field_validator("account_labels")
    @classmethod
    def _valid_account_labels(cls, labels: dict[str, str]) -> dict[str, str]:
        if any(not account_id or not label.strip() or len(label.strip()) > 80 for account_id, label in labels.items()):
            raise ValueError("account labels must be nonblank and at most 80 characters")
        return {account_id: label.strip() for account_id, label in labels.items()}

    @model_validator(mode="after")
    def _unique_account_ids(self) -> SpendConfiguration:
        account_ids = [card.account_id for card in self.cards]
        if len(account_ids) != len(set(account_ids)):
            raise ValueError("cards must contain at most one item per account_id")
        if any(account_id in account_ids for account_id in self.account_labels):
            raise ValueError("account_labels must not duplicate a configured card label")
        if self.account_labels.keys() - (self.allowance.spending_account_ids if self.allowance else set()):
            raise ValueError("account_labels must refer to spending accounts")
        return self


def load_configuration(path: Path) -> SpendConfiguration:
    """Load and validate the shared YAML configuration used by the app and analyses."""
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    return SpendConfiguration.model_validate(document)


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
    activation_at: date
    currency: str
    spending_account_count: int
    max_sync_age_hours: int
    rules: list[Rule]
    analysis_category_labels: dict[str, str] = Field(default_factory=dict)


class SpendConfigurationView(BaseModel):
    """Configuration currently loaded by the service, omitting all account IDs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cards: list[CardConfigurationView]
    allowance: AllowanceConfigurationView | None


class CardView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    account_id: str
    label: str
    account_name: str
    institution_name: str | None
    mask: str | None
    currency: str
    cycle_start: date | None
    spend_minor_units: int | None
    posted_minor_units: int | None
    pending_minor_units: int | None
    limit_minor_units: int | None
    alert_threshold_percent: int | None
    spend_percent: float | None
    alert_state: AlertState
    last_synced_at: datetime_type | None
    statement_available: bool


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
    authorized_date: date | None = None
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


class SpendTransactionRow(BaseModel):
    """A read-only explanation plus named, typed Plaid source fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    date: date
    account_label: str
    name: str
    merchant_name: str | None
    amount_minor_units: int
    currency: str
    pending: bool
    allowance_in_scope: bool
    disposition: Disposition | None
    rule_number: int | None
    rule: Rule | None
    allowance_minor_units: int
    trailing_7_pace_minor_units: int
    trailing_30_pace_minor_units: int
    statement_minor_units: int | None
    statement_reason: StatementReason | None
    pfc_primary: str | None
    pfc_detailed: str | None
    merchant_category_code: str | None
    counterparties: list[PlaidCounterparty] = Field(default_factory=list)
    details: PlaidTransactionDetails = Field(default_factory=PlaidTransactionDetails)


class SpendTransactionsView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    generated_at: datetime_type
    window: Literal["7d", "30d", "cycle"]
    window_start: date
    allowance: AllowanceView | None
    rows: list[SpendTransactionRow]
