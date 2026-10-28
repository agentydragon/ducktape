"""Strict configuration and response models for current portfolio values."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class HoldingValueCorrection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    account_id: str = Field(min_length=1)
    security_ids: list[str] = Field(min_length=1)
    value_multiplier: Decimal = Field(gt=0, allow_inf_nan=False)

    @field_validator("security_ids")
    @classmethod
    def _unique_security_ids(cls, values: list[str]) -> list[str]:
        if any(not value for value in values) or len(values) != len(set(values)):
            raise ValueError("security selectors must contain unique non-empty IDs")
        return values

    @field_validator("value_multiplier", mode="before")
    @classmethod
    def _decimal_multiplier(cls, value: object) -> Decimal:
        if isinstance(value, bool) or not isinstance(value, int | Decimal):
            raise ValueError("value_multiplier must be a positive decimal or integer")
        return Decimal(value)


class PlaidSourcePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    asset_account_ids: list[str] = Field(default_factory=list)
    liability_account_ids: list[str] = Field(default_factory=list)
    max_age_hours: int = Field(default=72, gt=0)
    holding_value_corrections: list[HoldingValueCorrection] = Field(default_factory=list)

    @field_validator("asset_account_ids", "liability_account_ids")
    @classmethod
    def _unique_nonempty_ids(cls, values: list[str]) -> list[str]:
        if any(not value for value in values) or len(values) != len(set(values)):
            raise ValueError("account selectors must contain unique non-empty IDs")
        return values

    @model_validator(mode="after")
    def _valid_holding_value_corrections(self) -> PlaidSourcePolicy:
        asset_ids = set(self.asset_account_ids)
        positions: set[tuple[str, str]] = set()
        for correction in self.holding_value_corrections:
            if correction.account_id not in asset_ids:
                raise ValueError("holding value corrections must belong to a selected asset account")
            for security_id in correction.security_ids:
                position = (correction.account_id, security_id)
                if position in positions:
                    raise ValueError("holding value correction positions must not overlap")
                positions.add(position)
        return self


class CoinbaseSourcePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    enabled: bool = False


class PortfolioSources(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    plaid: PlaidSourcePolicy
    coinbase: CoinbaseSourcePolicy = Field(default_factory=CoinbaseSourcePolicy)

    @model_validator(mode="after")
    def _disjoint_account_selectors(self) -> PortfolioSources:
        if set(self.plaid.asset_account_ids) & set(self.plaid.liability_account_ids):
            raise ValueError("asset and liability account selectors must be disjoint")
        return self


class ExcludedSecurity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    label: str = Field(min_length=1)
    source: Literal["plaid"]
    account_id: str = Field(min_length=1)
    security_ids: list[str] = Field(min_length=1)

    @field_validator("security_ids")
    @classmethod
    def _unique_security_ids(cls, values: list[str]) -> list[str]:
        if any(not value for value in values) or len(values) != len(set(values)):
            raise ValueError("security selectors must contain unique non-empty IDs")
        return values


class PortfolioPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    sources: PortfolioSources
    excluded_securities: list[ExcludedSecurity] = Field(default_factory=list)

    @model_validator(mode="after")
    def _valid_exclusions(self) -> PortfolioPolicy:
        asset_ids = set(self.sources.plaid.asset_account_ids)
        labels: set[str] = set()
        positions: set[tuple[str, str]] = set()
        for exclusion in self.excluded_securities:
            if exclusion.account_id not in asset_ids:
                raise ValueError("excluded securities must belong to a selected asset account")
            if exclusion.label in labels:
                raise ValueError("exclusion labels must be unique")
            labels.add(exclusion.label)
            for security_id in exclusion.security_ids:
                position = (exclusion.account_id, security_id)
                if position in positions:
                    raise ValueError("excluded security positions must not overlap")
                positions.add(position)
        return self


class SourceValuation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str
    status: Literal["ready", "unavailable"]
    amount_minor_units: int | None
    as_of: datetime | None
    issues: list[str] = Field(default_factory=list)


class PortfolioView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: Literal["ready", "unavailable"]
    net_minor_units: int | None
    assets_minor_units: int | None
    liabilities_minor_units: int | None
    excluded_minor_units: int | None
    sources: list[SourceValuation]
