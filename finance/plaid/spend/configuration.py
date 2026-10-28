"""Validated server-side Spend policy and YAML configuration loading."""

from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from finance.plaid.spend.allowance import AllowancePolicy
from finance.plaid.spend.sustainability import SustainabilityPolicy
from finance.yaml_decimal import DecimalSafeLoader


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
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)

    time_zone: ZoneInfo
    cards: list[CardConfig]
    allowance: AllowancePolicy | None = None
    sustainability: SustainabilityPolicy | None = None
    account_labels: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _sustainability_uses_shared_classification(self) -> SpendConfiguration:
        if self.sustainability is not None:
            if self.allowance is None:
                raise ValueError("sustainability requires shared allowance classifications")
            categories = set(self.sustainability.include_excluded_categories)
            if categories - self.allowance.analysis_categories.keys():
                raise ValueError("sustainability category overrides must refer to configured categories")
            rule_ids = {rule.id for rule in self.allowance.rules if rule.id is not None}
            if set(self.sustainability.confirmed_refund_rule_ids) - rule_ids:
                raise ValueError("confirmed refunds must refer to configured rule IDs")
            if self.sustainability.account_history.keys() - self.allowance.spending_account_ids:
                raise ValueError("account history evidence must refer to selected spending accounts")
        return self

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
    document = yaml.load(path.read_text(encoding="utf-8"), Loader=DecimalSafeLoader)
    return SpendConfiguration.model_validate(document)
