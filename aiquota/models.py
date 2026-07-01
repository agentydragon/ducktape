from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field


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


class ExtraSpend(BaseModel):
    """Internal summary of billable spend above the subscription quota."""

    is_enabled: bool
    monthly_limit_usd: float
    used_usd: float
    utilization: float


class FetchSuccess(BaseModel):
    """Payload from a quota fetch that returned without error.

    All three fields default to None — providers sometimes respond 200 but
    omit specific window data (e.g. codex on a fresh account returns no
    rate_limit). That's still "success with no data", not an error.
    """

    kind: Literal["success"] = "success"
    short_window: QuotaWindow | None = None
    long_window: QuotaWindow | None = None
    extra_spend: ExtraSpend | None = None


class FetchError(BaseModel):
    kind: Literal["error"] = "error"
    error: str

    @classmethod
    def from_exception(cls, e: BaseException, context: str | None = None) -> "FetchError":
        message = str(e).strip() or type(e).__name__
        if context:
            message = f"{context}: {message}"
        return cls(error=message)


_FetchResult = Annotated[FetchSuccess | FetchError, Field(discriminator="kind")]


class ProviderFetch(BaseModel):
    fetched_at: datetime
    result: _FetchResult


class SuccessfulProviderFetch(BaseModel):
    """A `ProviderFetch` whose result is statically known to be `FetchSuccess`.

    Lets `ProviderQuota.last_success` express the "must have succeeded"
    invariant in the type system instead of via runtime convention.
    """

    fetched_at: datetime
    result: FetchSuccess


class ProviderQuota(BaseModel):
    provider: str
    last_output: ProviderFetch
    last_success: SuccessfulProviderFetch | None = None


class AllQuotas(BaseModel):
    providers: list[ProviderQuota]
    fetched_at: datetime
