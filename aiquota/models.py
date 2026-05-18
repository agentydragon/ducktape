from datetime import datetime

from pydantic import BaseModel


class QuotaWindow(BaseModel):
    used_percent: float
    reset_seconds: float
    window_seconds: float
    reset_at: datetime | None = None


class PaceResult(BaseModel):
    deviation: float
    projected_at_reset: float | None
    seconds_to_exhaust: float | None
    stable: bool


class ExtraUsage(BaseModel):
    is_enabled: bool
    monthly_limit_usd: float
    used_usd: float
    utilization: float


class ProviderFetch(BaseModel):
    """One quota-fetch result for a single provider.

    Used for both `last_output` (what the most recent call returned) and
    `last_success` (the most recent call that produced usable windows). The
    `error` field is naturally `None` when a `ProviderFetch` represents a
    successful fetch.
    """

    short_window: QuotaWindow | None = None
    long_window: QuotaWindow | None = None
    extra_usage: ExtraUsage | None = None
    error: str | None = None
    fetched_at: datetime


class ProviderQuota(BaseModel):
    provider: str
    last_output: ProviderFetch
    # The newest `last_output` whose error was None and which produced at least
    # one window. May be the same instance as `last_output` (current fetch
    # succeeded) or older (current fetch errored — fall back for display).
    last_success: ProviderFetch | None = None


class AllQuotas(BaseModel):
    providers: list[ProviderQuota]
    fetched_at: datetime
