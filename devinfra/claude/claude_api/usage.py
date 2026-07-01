"""Client and models for the Claude subscription usage API."""

from datetime import datetime
from typing import Self

import httpx
from pydantic import BaseModel, ConfigDict, model_validator

# Undocumented Claude Code-specific endpoint for subscription utilization
# (5-hour / 7-day quotas). Not part of the official Anthropic Python SDK,
# which only exposes per-message token usage (anthropic.types.Usage).
_USAGE_API_URL = "https://api.anthropic.com/api/oauth/usage"
_API_TIMEOUT_SECONDS = 2.0


class UsageBucket(BaseModel):
    model_config = ConfigDict(extra="ignore")

    utilization: float
    resets_at: datetime | None = None


class ExtraUsage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    is_enabled: bool
    monthly_limit: float | None = None
    used_credits: float | None = None
    utilization: float | None = None
    currency: str | None = None
    disabled_reason: str | None = None

    @model_validator(mode="after")
    def _enabled_requires_money(self) -> Self:
        if self.is_enabled and (self.monthly_limit is None or self.used_credits is None):
            raise ValueError("enabled extra_usage requires monthly_limit and used_credits")
        return self


class MoneyAmount(BaseModel):
    model_config = ConfigDict(extra="ignore")

    amount_minor: float
    exponent: int = 2
    currency: str | None = None


class Spend(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool | None = None
    limit: MoneyAmount | None = None
    used: MoneyAmount | None = None
    percent: float | None = None
    severity: str | None = None
    disabled_reason: str | None = None


class ExtraUsageTotals(BaseModel):
    is_enabled: bool = True
    monthly_limit: float
    used_credits: float
    utilization: float
    currency: str | None = None


class UsageResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    five_hour: UsageBucket | None = None
    seven_day: UsageBucket | None = None
    seven_day_opus: UsageBucket | None = None
    seven_day_sonnet: UsageBucket | None = None
    extra_usage: ExtraUsage | None = None
    spend: Spend | None = None


def _major_to_minor_units(amount: MoneyAmount) -> float:
    return amount.amount_minor * (100 / (10**amount.exponent))


def _utilization(used_credits: float, monthly_limit: float, explicit: float | None) -> float:
    if explicit is not None:
        return explicit
    return used_credits / monthly_limit * 100 if monthly_limit > 0 else 0.0


def normalized_extra_usage(usage: UsageResponse) -> ExtraUsageTotals | None:
    spend = usage.spend
    if spend is not None:
        if spend.enabled is False:
            return None
        if spend.enabled is True and spend.limit is not None and spend.used is not None:
            monthly_limit = _major_to_minor_units(spend.limit)
            used_credits = _major_to_minor_units(spend.used)
            return ExtraUsageTotals(
                monthly_limit=monthly_limit,
                used_credits=used_credits,
                utilization=_utilization(used_credits, monthly_limit, spend.percent),
                currency=spend.used.currency or spend.limit.currency,
            )

    extra = usage.extra_usage
    if extra is None or not extra.is_enabled:
        return None
    assert extra.monthly_limit is not None
    assert extra.used_credits is not None
    return ExtraUsageTotals(
        monthly_limit=extra.monthly_limit,
        used_credits=extra.used_credits,
        utilization=_utilization(extra.used_credits, extra.monthly_limit, extra.utilization),
        currency=extra.currency,
    )


def fetch_usage(token: str) -> UsageResponse:
    """Fetch subscription usage from the Claude API."""
    response = httpx.get(
        _USAGE_API_URL,
        headers={"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"},
        timeout=_API_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return UsageResponse.model_validate(response.json())
