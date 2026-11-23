"""Validated server-side Spend policy and YAML configuration loading."""

from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from finance.plaid.spend.account_history import AccountHistory
from finance.plaid.spend.allowance import AllowancePolicy
from finance.plaid.spend.models import AllowanceConfigurationView, CardConfigurationView, SpendConfigurationView
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
    account_history: dict[str, AccountHistory] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _lift_legacy_account_history(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        sustainability = value.get("sustainability")
        if not isinstance(sustainability, dict) or "account_history" not in sustainability:
            return value
        if "account_history" in value:
            raise ValueError("specify account_history once at the root of the Spend configuration")
        updated_sustainability = dict(sustainability)
        history = updated_sustainability.pop("account_history")
        return {**value, "sustainability": updated_sustainability, "account_history": history}

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


def configuration_view(configuration: SpendConfiguration) -> SpendConfigurationView:
    policy = configuration.allowance
    allowance = None
    if policy is not None:
        allowance = AllowanceConfigurationView(
            monthly_minor_units=policy.monthly_minor_units,
            activation_at=policy.activation_at,
            currency=policy.currency,
            spending_account_count=len(policy.spending_account_ids),
            max_sync_age_hours=policy.max_sync_age_hours,
            forecast_basis_period_id=policy.forecast_basis_period_id,
            rules=policy.rules,
            reversal_pairs=policy.reversal_pairs,
            analysis_categories=policy.analysis_categories,
        )
    return SpendConfigurationView(
        time_zone=configuration.time_zone.key,
        cards=[
            CardConfigurationView(
                label=card.label,
                enabled=card.enabled,
                limit_minor_units=card.limit_minor_units,
                alert_threshold_percent=card.alert_threshold_percent,
            )
            for card in configuration.cards
        ],
        allowance=allowance,
    )
