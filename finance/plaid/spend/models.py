"""Validated Plaid spend configuration and API views."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class AlertState(StrEnum):
    NORMAL = "normal"
    WARNING = "warning"
    EXCEEDED = "exceeded"
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


class CardConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    cards: list[CardConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_account_ids(self) -> CardConfiguration:
        account_ids = [card.account_id for card in self.cards]
        if len(account_ids) != len(set(account_ids)):
            raise ValueError("cards must contain at most one item per account_id")
        return self


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
    last_synced_at: datetime | None
    statement_available: bool


class SpendView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    generated_at: datetime
    cards: list[CardView]
