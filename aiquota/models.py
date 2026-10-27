from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator


class QuotaWindow(BaseModel):
    name: str | None = None
    display: bool = True
    used_percent: float
    reset_seconds: float = Field(ge=0)
    window_seconds: float = Field(gt=0)
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


class PaidCredits(BaseModel):
    """Codex usage credits, separate from earned rate-limit resets."""

    balance: str | None = None
    unlimited: bool = False


class PrepaidBalanceAvailable(BaseModel):
    """An exact provider-reported prepaid monetary balance."""

    kind: Literal["available"] = "available"
    amount_minor: int
    exponent: int = Field(ge=0, le=9)
    currency: str


class PrepaidBalanceUnavailable(BaseModel):
    """A supplemental prepaid balance lookup failed after usage succeeded."""

    kind: Literal["unavailable"] = "unavailable"


PrepaidBalance = Annotated[PrepaidBalanceAvailable | PrepaidBalanceUnavailable, Field(discriminator="kind")]


class FetchSuccess(BaseModel):
    """Payload from a quota fetch that returned without error.

    Providers sometimes respond 200 but omit window data (e.g. codex on a
    fresh account returns no rate_limit). That's still "success with no
    data", not an error.
    """

    kind: Literal["success"] = "success"
    windows: list[QuotaWindow] = Field(default_factory=list)
    extra_spend: ExtraSpend | None = None
    paid_credits: PaidCredits | None = None
    # Claude's prepaid monetary balance is a separate endpoint and denomination
    # from Codex paid credits. None means this provider does not report it.
    prepaid_balance: PrepaidBalance | None = None
    # Earned Codex rate-limit resets. None means the provider did not publish a
    # count; 0 is an authoritative "none available" response.
    available_reset_credits: int | None = Field(default=None, ge=0)
    # Expiries from the best-effort reset-credit detail endpoint. This can be
    # incomplete even when `available_reset_credits` is authoritative.
    available_reset_credit_expiries: list[datetime] = Field(default_factory=list)
    # True only when the supplemental detail lookup or its response parsing
    # failed. An empty expiry list on success does not imply lookup failure.
    reset_credit_expiry_lookup_failed: bool = False

    @field_validator("windows")
    @classmethod
    def sort_unique_windows(cls, windows: list[QuotaWindow]) -> list[QuotaWindow]:
        identities = [(window.name, window.window_seconds) for window in windows]
        if len(identities) != len(set(identities)):
            raise ValueError("quota window identities must be unique")
        return sorted(windows, key=lambda window: (window.window_seconds, window.name is not None, window.name or ""))


class FetchError(BaseModel):
    kind: Literal["error"] = "error"
    error: str

    @classmethod
    def from_exception(cls, e: BaseException, context: str | None = None) -> FetchError:
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


class TokenActivityDay(BaseModel):
    """Account-wide tokens the provider attributes to one past day."""

    start_date: date
    tokens: int


class ResetCredit(BaseModel):
    """One rate-limit reset credit the provider has granted to the account."""

    credit_id: str
    reset_type: str
    status: str
    granted_at: datetime
    expires_at: datetime | None = None


class HistoryKind(StrEnum):
    TOKEN_ACTIVITY = "token_activity"
    RESET_CREDITS = "reset_credits"


class TokenActivityObservation(BaseModel):
    kind: Literal[HistoryKind.TOKEN_ACTIVITY] = HistoryKind.TOKEN_ACTIVITY
    days: list[TokenActivityDay]


class ResetCreditsObservation(BaseModel):
    kind: Literal[HistoryKind.RESET_CREDITS] = HistoryKind.RESET_CREDITS
    credits: list[ResetCredit]


_HistoryPayload = Annotated[TokenActivityObservation | ResetCreditsObservation, Field(discriminator="kind")]


class HistoryObservation(BaseModel):
    """One reading of a provider endpoint describing the past, not the present.

    Quota windows say how full the account is right now; these endpoints report
    days or grants that already happened, so a single reading backfills history
    that predates aiquota's own collection.
    """

    provider: str
    observed_at: datetime
    payload: _HistoryPayload


def history_capture_key(provider: str, kind: HistoryKind) -> str:
    """Name under which one history endpoint's raw response is captured.

    Raw capture is keyed per endpoint, not per provider: a provider polling
    several endpoints in one cycle would otherwise overwrite its own bodies.
    """

    return f"{provider}_{kind}"


class RawUpstreamResponse(BaseModel):
    """The safely exposable parts of one quota-endpoint response.

    Request and response headers are deliberately excluded: they can contain
    credentials, cookies, account identifiers, or proxy implementation data.
    """

    status_code: int
    content_type: str | None = None
    body: object | None = None
    body_base64: str | None = None
    body_sha256: str | None = None
    body_size_bytes: int | None = None
    truncated: bool = False


@dataclass(frozen=True)
class QuotaSnapshot:
    quotas: AllQuotas
    raw_responses: dict[str, RawUpstreamResponse]


@dataclass(frozen=True)
class HistorySnapshot:
    observations: list[HistoryObservation]
    fetched_at: datetime
    raw_responses: dict[str, RawUpstreamResponse]
