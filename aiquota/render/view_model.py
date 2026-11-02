"""Presentation decisions shared by the API, CLI, browser and GNOME."""

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal, cast

from pydantic import BaseModel

from aiquota.models import (
    AllQuotas,
    FetchSuccess,
    PlatformCredits,
    ProviderFetch,
    ProviderQuota,
    QuotaWindow,
    SuccessfulProviderFetch,
)
from aiquota.pace import TINT_RANK, compute_pace, is_exhausted, tint_for
from aiquota.peak_windows import BurnStatus, status_for
from aiquota.render.format import (
    display_used_percent,
    format_duration,
    format_pace,
    format_pace_forecast,
    format_window_label,
)

ExtraStatus = Literal["none", "informational", "active"]


Tint = Literal["cool", "ok", "warn", "hot", "unknown", "stale"]


class WindowView(QuotaWindow):
    label: str
    used: int
    reset: str
    pace: str | None
    forecast: str | None
    tint: Tint
    elapsed: float
    exhausted: bool


class ProviderView(BaseModel):
    provider: str
    last_output: ProviderFetch
    last_success: SuccessfulProviderFetch | None
    currently_over_plan: bool
    extra_status: ExtraStatus
    paid_credits_active: bool
    burn: BurnStatus | None
    display_windows: list[WindowView]
    display_platform_credits: PlatformCredits | None = None
    summary_tint: Tint
    summary_index: int | None


class AllQuotasView(BaseModel):
    fetched_at: datetime
    providers: list[ProviderView]


def to_view(quotas: AllQuotas, now: datetime | None = None) -> AllQuotasView:
    moment = now or datetime.now(UTC)
    return AllQuotasView(
        fetched_at=quotas.fetched_at, providers=[_provider_view(pq, moment) for pq in quotas.providers]
    )


def _provider_view(pq: ProviderQuota, now: datetime) -> ProviderView:
    result = pq.last_output.result
    source: FetchSuccess | None
    if isinstance(result, FetchSuccess) and (
        result.windows
        or result.available_reset_credits is not None
        or result.paid_credits is not None
        or result.platform_credits is not None
        or result.prepaid_balance is not None
    ):
        source = result
    else:
        source = pq.last_success.result if pq.last_success is not None else None
    windows = [window for window in source.windows if window.display] if source is not None else []
    longest = max((window.window_seconds for window in windows), default=0)
    display_windows = [_window_view(window, now, window.window_seconds < longest) for window in windows]
    tint: Tint = "unknown"
    for window in display_windows:
        if TINT_RANK[window.tint] > TINT_RANK[tint]:
            tint = window.tint
    preferred = next((i for i in reversed(range(len(windows))) if windows[i].name is None), len(windows) - 1)
    index = None
    if windows:
        index = (
            preferred
            if display_windows[preferred].tint == tint
            else next((i for i, window in enumerate(display_windows) if window.tint == tint), None)
        )
    credits = source.platform_credits if source is not None else None
    # OpenAI's billing backend returns fully used grants with expires_at=null;
    # its billing page calls them inactive. Hide depleted and expired grants,
    # but keep scheduled and held grants unless their reported expiry has passed.
    # Filter a copy so the underlying provider response remains available.
    display_credits = (
        credits.model_copy(
            update={
                "grants": [
                    grant
                    for grant in credits.grants
                    if (grant.expires_at is None or grant.expires_at > now)
                    and (
                        grant.used_usd < grant.granted_usd or grant.is_scheduled or grant.is_held_for_prepaid_transition
                    )
                ]
            }
        )
        if credits is not None
        else None
    )
    return ProviderView(
        provider=pq.provider,
        last_output=pq.last_output,
        last_success=pq.last_success,
        currently_over_plan=currently_over_plan(pq.last_output),
        extra_status=_extra_status(pq.last_output),
        paid_credits_active=_paid_credits_active(pq.last_output),
        burn=status_for(pq.provider, now),
        display_windows=display_windows,
        display_platform_credits=display_credits,
        summary_tint=tint,
        summary_index=index,
    )


def _window_view(window: QuotaWindow, now: datetime, is_short: bool) -> WindowView:
    if window.reset_at is not None:
        window = window.model_copy(update={"reset_seconds": max(0.0, (window.reset_at - now).total_seconds())})
    exhausted = is_exhausted(window)
    pace = None if exhausted else compute_pace(window)
    return WindowView(
        **window.model_dump(),
        label=format_window_label(window),
        used=display_used_percent(window),
        reset=format_duration(window.reset_seconds),
        pace=format_pace(pace),
        forecast="exhausted" if exhausted else format_pace_forecast(pace, window.reset_seconds),
        tint="hot" if exhausted else cast(Tint, tint_for(pace, window.used_percent, is_short=is_short)),
        elapsed=max(0.0, min(1.0, (window.window_seconds - window.reset_seconds) / window.window_seconds)),
        exhausted=exhausted,
    )


def currently_over_plan(out: ProviderFetch) -> bool:
    """True when the user is actively paying USD above subscription right now.

    `ExtraSpend.is_enabled` only signals "feature enabled on the account",
    and `ExtraSpend.used_usd` is a cumulative monthly tally — neither says
    anything about "right now". The real signal is *any* rate-limit window
    being exhausted (every further call now hits the monthly bill).
    """
    if not isinstance(out.result, FetchSuccess):
        return False
    extra = out.result.extra_spend
    if extra is None or not extra.is_enabled:
        return False
    return any(is_exhausted(window) for window in out.result.windows)


def _extra_status(out: ProviderFetch) -> ExtraStatus:
    if currently_over_plan(out):
        return "active"
    if not isinstance(out.result, FetchSuccess):
        return "none"
    extra = out.result.extra_spend
    if extra is not None and extra.is_enabled and extra.used_usd > 0:
        return "informational"
    return "none"


def _paid_credits_active(out: ProviderFetch) -> bool:
    if not isinstance(out.result, FetchSuccess) or out.result.paid_credits is None:
        return False
    credits = out.result.paid_credits
    if not credits.unlimited and credits.balance is not None:
        try:
            if Decimal(credits.balance) <= 0:
                return False
        except InvalidOperation:
            return False
    return any(is_exhausted(window) for window in out.result.windows if window.display)
