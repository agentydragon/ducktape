"""Pydantic models for the Claude subscription usage API (5-hour / 7-day quotas).

The actual fetch (with OAuth token refresh) lives in `aiquota.providers.claude`.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class UsageBucket(BaseModel):
    model_config = ConfigDict(extra="ignore")

    utilization: float
    resets_at: datetime | None = None


class MoneyAmount(BaseModel):
    model_config = ConfigDict(extra="ignore")

    amount_minor: float
    exponent: int = 2
    currency: str | None = None

    @property
    def major_units(self) -> float:
        # `10 ** int` returns `int | Any` (typeshed overloads a negative-
        # exponent branch that returns float), so `float / (10**int)` widens
        # to `Any` and trips mypy's warn_return_any. `10.0 ** int` returns
        # float unconditionally, keeping the division typed.
        return self.amount_minor / (10.0**self.exponent)


class Spend(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool | None = None
    limit: MoneyAmount | None = None
    used: MoneyAmount | None = None
    percent: float | None = None
    severity: str | None = None
    disabled_reason: str | None = None

    @property
    def has_usage_totals(self) -> bool:
        return self.enabled is True and self.limit is not None and self.used is not None

    @property
    def utilization_percent(self) -> float:
        if self.percent is not None:
            return self.percent
        if self.limit is None or self.used is None or self.limit.amount_minor <= 0:
            return 0.0
        return self.used.amount_minor / self.limit.amount_minor * 100


class UsageResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    five_hour: UsageBucket | None = None
    seven_day: UsageBucket | None = None
    seven_day_opus: UsageBucket | None = None
    seven_day_sonnet: UsageBucket | None = None
    spend: Spend | None = None
