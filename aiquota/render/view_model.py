"""Presentation decisions shared by the API, CLI, browser and GNOME."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Literal, cast

from pydantic import BaseModel, Field

from aiquota.models import (
    AllQuotas,
    AnthropicPlatformCredits,
    AnthropicPlatformTranche,
    ExtraSpend,
    FetchSuccess,
    OpenAIPlatformCredits,
    PrepaidBalance,
    ProviderFetch,
    ProviderQuota,
    QuotaWindow,
    SuccessfulProviderFetch,
    is_substantive,
)
from aiquota.pace import TINT_RANK, compute_pace, is_exhausted, tint_for
from aiquota.peak_windows import BurnStatus, status_for
from aiquota.render.format import (
    display_used_percent,
    format_duration,
    format_minor_currency,
    format_pace,
    format_pace_forecast,
    format_paid_credit_balance,
    format_prepaid_balance,
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
    reset_pending: bool = False


class DisplaySelection(BaseModel):
    source: Literal["current", "last_success", "none"]
    observed_at: datetime | None
    result: FetchSuccess | None
    prepaid_balance: PrepaidBalance | None = None
    prepaid_stale_since: datetime | None = None


class BalanceView(BaseModel):
    label: str
    value: str
    group: str | None = None
    stale_since: datetime | None = None
    error: str | None = None


class GrantView(BaseModel):
    label: str
    remaining: str
    original: str
    expires_at: datetime | None
    scope: str | None = None
    scope_detail: str | None = None
    status: str | None = None
    group: str | None = None


class ProviderView(BaseModel):
    provider: str
    label: str = ""
    balances: list[BalanceView] = Field(default_factory=list)
    grants: list[GrantView] = Field(default_factory=list)
    stale_since: datetime | None = None
    stale_at: datetime | None = None
    status: str | None = None
    error_summary: str | None = None
    error_detail: str | None = None
    display: DisplaySelection
    last_output: ProviderFetch
    last_success: SuccessfulProviderFetch | None
    currently_over_plan: bool
    extra_status: ExtraStatus
    monthly_extra_spend: ExtraSpend | None = None
    paid_credits_active: bool
    burn: BurnStatus | None
    display_windows: list[WindowView]
    display_openai_platform_credits: OpenAIPlatformCredits | None = None
    display_anthropic_platform_credits: AnthropicPlatformCredits | None = None
    summary_tint: Tint
    summary_index: int | None


class AllQuotasView(BaseModel):
    connection_error: str | None = None
    presentation_at: datetime | None = None
    fetched_at: datetime
    providers: list[ProviderView]


def to_view(quotas: AllQuotas, now: datetime | None = None) -> AllQuotasView:
    moment = now or datetime.now(UTC)
    return AllQuotasView(
        fetched_at=quotas.fetched_at,
        presentation_at=moment,
        providers=[_provider_view(pq, moment) for pq in quotas.providers],
    )


def refresh_view(
    view: AllQuotasView, now: datetime | None = None, *, connection_error: str | None = None
) -> AllQuotasView:
    moment = now or datetime.now(UTC)
    return AllQuotasView(
        fetched_at=view.fetched_at,
        presentation_at=moment,
        connection_error=connection_error,
        providers=[
            _provider_view(
                ProviderQuota(provider=pv.provider, last_output=pv.last_output, last_success=pv.last_success),
                moment,
                selection=pv.display,
                offline=connection_error is not None,
            )
            for pv in view.providers
        ],
    )


def _select_display(pq: ProviderQuota) -> DisplaySelection:
    result = pq.last_output.result
    if isinstance(result, FetchSuccess) and is_substantive(result):
        selection = DisplaySelection(source="current", observed_at=pq.last_output.fetched_at, result=result)
    elif pq.last_success is not None:
        selection = DisplaySelection(
            source="last_success", observed_at=pq.last_success.fetched_at, result=pq.last_success.result
        )
    else:
        selection = DisplaySelection(source="none", observed_at=None, result=None)
    # Prepaid is a separate endpoint; preserve the CLI's per-field fallback.
    if isinstance(result, FetchSuccess) and result.prepaid_balance is not None:
        selection.prepaid_balance = result.prepaid_balance
    elif pq.last_success is not None and pq.last_success.result.prepaid_balance is not None:
        selection.prepaid_balance = pq.last_success.result.prepaid_balance
        selection.prepaid_stale_since = pq.last_success.fetched_at
    return selection


_PROVIDER_LABELS = {
    "claude": "Claude",
    "codex": "Codex",
    "zai": "z.ai",
    "openai_platform": "OpenAI API",
    "anthropic_platform": "Anthropic API",
}
# Two collector intervals avoid a stale warning during the normal five-minute poll boundary.
_STALE_AFTER = timedelta(minutes=10)
_PRODUCT_LABELS = {"claude_api": "Claude API", "claude_code": "Claude Code"}
# The broad API promo scope in the observed account fixture. Unknown products or
# model-specific qualifiers remain visible restrictions, never inferred as universal.
_BROAD_API_PRODUCTS = {"agent_sdk", "api", "batch", "console", "managed-agents", "managed-agents-onprem"}


def _provider_view(
    pq: ProviderQuota, now: datetime, *, selection: DisplaySelection | None = None, offline: bool = False
) -> ProviderView:
    selection = selection if selection is not None else _select_display(pq)
    observed_at = selection.observed_at
    stale_since = (
        observed_at
        if observed_at is not None
        and (offline or selection.source == "last_success" or now - observed_at >= _STALE_AFTER)
        else None
    )
    source = selection.result
    windows = [window for window in source.windows if window.display] if source is not None else []
    longest = max((window.window_seconds for window in windows), default=0)
    display_windows = [
        _window_view(window, now, window.window_seconds < longest, observed_at, stale_since is not None)
        for window in windows
    ]
    tint: Tint = "unknown"
    for window in display_windows:
        if tint == "unknown" or TINT_RANK[window.tint] > TINT_RANK[tint]:
            tint = window.tint
    preferred = next((i for i in reversed(range(len(windows))) if windows[i].name is None), len(windows) - 1)
    index = None
    if windows:
        index = (
            preferred
            if display_windows[preferred].tint == tint
            else next((i for i, window in enumerate(display_windows) if window.tint == tint), None)
        )
    openai_platform_credits = source.openai_platform_credits if source is not None else None
    # OpenAI's billing backend returns fully used grants with expires_at=null;
    # its billing page calls them inactive. Hide depleted and expired grants,
    # but keep scheduled and held grants unless their reported expiry has passed.
    # Filter a copy so the underlying provider response remains available.
    display_openai_platform_credits = (
        openai_platform_credits.model_copy(
            update={
                "grants": [
                    grant
                    for grant in openai_platform_credits.grants
                    if (grant.expires_at is None or grant.expires_at > now)
                    and (
                        grant.used_usd < grant.granted_usd or grant.is_scheduled or grant.is_held_for_prepaid_transition
                    )
                ]
            }
        )
        if openai_platform_credits is not None
        else None
    )
    anthropic_credits = source.anthropic_platform_credits if source is not None else None
    display_anthropic_credits = (
        anthropic_credits.model_copy(
            update={
                "organizations": [
                    organization.model_copy(
                        update={
                            "tranches": _active_anthropic_tranches(organization.tranches, now),
                            "promo_tranches": _active_anthropic_tranches(organization.promo_tranches, now),
                        }
                    )
                    for organization in anthropic_credits.organizations
                ]
            }
        )
        if anthropic_credits is not None
        else None
    )
    balances, grants = _credit_rows(selection, display_openai_platform_credits, display_anthropic_credits, stale_since)
    grants.extend(_reset_grant_rows(source, now))
    reset_pending = any(window.reset_pending for window in display_windows)
    activity_current = stale_since is None and not reset_pending
    over_plan = activity_current and currently_over_plan(pq.last_output)
    paid_active = activity_current and _paid_credits_active(pq.last_output)
    extra = source.extra_spend if source is not None else None
    monthly_extra = extra if extra is not None and extra.used_usd > 0 else None
    status = None
    if reset_pending:
        status = "Reset time passed · awaiting updated usage"
    elif any(window.exhausted for window in display_windows):
        status = "Included quota exhausted"
        if over_plan:
            status += " · extra usage enabled"
        elif paid_active:
            status += " · paid credits available"
    error = pq.last_output.result.error if not isinstance(pq.last_output.result, FetchSuccess) else None
    return ProviderView(
        provider=pq.provider,
        error_summary=_error_summary(error),
        error_detail=error,
        label=_PROVIDER_LABELS.get(pq.provider, pq.provider),
        balances=balances,
        grants=grants,
        stale_since=stale_since,
        stale_at=observed_at + _STALE_AFTER if observed_at is not None else None,
        status=status,
        display=selection,
        last_output=pq.last_output,
        last_success=pq.last_success,
        currently_over_plan=over_plan,
        extra_status="active" if over_plan else "informational" if monthly_extra is not None else "none",
        monthly_extra_spend=monthly_extra,
        paid_credits_active=paid_active,
        burn=status_for(pq.provider, now),
        display_windows=display_windows,
        display_openai_platform_credits=display_openai_platform_credits,
        display_anthropic_platform_credits=display_anthropic_credits,
        summary_tint=tint,
        summary_index=index,
    )


def _error_summary(error: str | None) -> str | None:
    if error is None:
        return None
    if error in {"no credentials found", "no codex auth found"}:
        return "Credentials unavailable"
    if error.startswith("HTTP 401") or "invalid_token" in error or "token refresh failed" in error:
        return "Authentication failed"
    return "Refresh failed"


def _active_anthropic_tranches(
    tranches: list[AnthropicPlatformTranche], now: datetime
) -> list[AnthropicPlatformTranche]:
    """Keep funded Anthropic tranches whose reported expiry is still in the future."""
    return [
        tranche
        for tranche in tranches
        if (tranche.expires_at is None or tranche.expires_at > now)
        and tranche.remaining.credits is not None
        and tranche.remaining.credits.amount_minor > 0
    ]


def _window_view(
    window: QuotaWindow, now: datetime, is_short: bool, observed_at: datetime | None, stale: bool
) -> WindowView:
    deadline = window.reset_at
    if deadline is None and observed_at is not None and window.reset_seconds > 0:
        deadline = observed_at + timedelta(seconds=window.reset_seconds)
    reset_pending = deadline is not None and deadline <= now
    if deadline is not None:
        window = window.model_copy(
            update={"reset_at": deadline, "reset_seconds": max(0.0, (deadline - now).total_seconds())}
        )
    exhausted = is_exhausted(window)
    pace = None if exhausted or reset_pending or stale else compute_pace(window)
    return WindowView(
        **window.model_dump(),
        label=format_window_label(window),
        used=display_used_percent(window),
        reset="awaiting update" if reset_pending else format_duration(window.reset_seconds),
        pace=format_pace(pace),
        forecast=None
        if reset_pending or stale
        else "exhausted"
        if exhausted
        else format_pace_forecast(pace, window.reset_seconds),
        tint="stale"
        if stale or reset_pending
        else "hot"
        if exhausted
        else cast(Tint, tint_for(pace, window.used_percent, is_short=is_short)),
        elapsed=max(0.0, min(1.0, (window.window_seconds - window.reset_seconds) / window.window_seconds)),
        exhausted=exhausted,
        reset_pending=reset_pending,
    )


# Keep the default visibility rule in sync with shared/presentation.ts.
def show_reset_credits(count: int | None, windows: list[WindowView], *, details: bool = False) -> bool:
    return count is not None and (
        count > 0
        or details
        or (any(window.exhausted for window in windows) and not any(window.reset_pending for window in windows))
    )


def _reset_grant_rows(result: FetchSuccess | None, now: datetime) -> list[GrantView]:
    if result is None:
        return []
    groups: dict[tuple[str, datetime | None, str | None], tuple[int, int]] = {}
    for grant in result.claude_reset_grants or []:
        if grant.remaining == 0 or (grant.expires_at is not None and grant.expires_at <= now):
            continue
        status = (
            "paused" if grant.paused else "scheduled" if grant.starts_at is not None and grant.starts_at > now else None
        )
        key = (grant.label, grant.expires_at, status)
        remaining, original = groups.get(key, (0, 0))
        groups[key] = (remaining + grant.remaining, original + grant.original)
    return [
        GrantView(label=label, remaining=str(remaining), original=str(original), expires_at=expiry, status=status)
        for (label, expiry, status), (remaining, original) in groups.items()
    ]


def _credit_rows(
    selection: DisplaySelection,
    openai: OpenAIPlatformCredits | None,
    anthropic: AnthropicPlatformCredits | None,
    stale_since: datetime | None,
) -> tuple[list[BalanceView], list[GrantView]]:
    balances: list[BalanceView] = []
    grants: list[GrantView] = []
    prepaid = selection.prepaid_balance
    if prepaid is not None:
        balances.append(
            BalanceView(
                label="Prepaid balance",
                value=format_prepaid_balance(prepaid) if prepaid.kind == "available" else "unavailable",
                stale_since=selection.prepaid_stale_since if selection.prepaid_stale_since != stale_since else None,
            )
        )
    result = selection.result
    if result is not None and result.paid_credits is not None:
        credits = result.paid_credits
        balances.append(
            BalanceView(label="Paid credits", value=format_paid_credit_balance(credits.balance, credits.unlimited))
        )
    if openai is not None:
        balances.append(BalanceView(label="Available balance", value=f"${openai.total_available_usd:,.2f}"))
        grants.extend(
            GrantView(
                label="Grant",
                remaining=f"${grant.granted_usd - grant.used_usd:,.2f}",
                original=f"${grant.granted_usd:,.2f}",
                expires_at=grant.expires_at,
                status="scheduled" if grant.is_scheduled else "held" if grant.is_held_for_prepaid_transition else None,
            )
            for grant in openai.grants
        )
    if anthropic is not None:
        for org in anthropic.organizations:
            balance = org.balance.credits if org.balance is not None else None
            value = (
                format_minor_currency(balance.amount_minor, balance.exponent, org.currency or "USD")
                if balance is not None
                else "unavailable"
            )
            if org.error is not None:
                value = "lookup failed"
            balances.append(BalanceView(label="Available balance", value=value, group=org.name, error=org.error))
            for label, tranches in (("Grant", org.tranches), ("Promo grant", org.promo_tranches)):
                for tranche in tranches:
                    remaining, original = tranche.remaining.credits, tranche.granted.credits
                    if remaining is None or original is None:
                        continue
                    products = sorted(
                        {
                            _scope_product(item.product, item.model_family, item.model_version)
                            for item in tranche.scope.applies_to
                        }
                    )
                    scope = ", ".join(products) or (
                        tranche.scope.kind if tranche.scope.kind not in {"unrestricted", "unscoped"} else None
                    )
                    broad_scope = (
                        tranche.scope.kind == "scoped"
                        and {item.product for item in tranche.scope.applies_to} == _BROAD_API_PRODUCTS
                        and all(
                            item.model_family is None and item.model_version is None
                            for item in tranche.scope.applies_to
                        )
                    )
                    grants.append(
                        GrantView(
                            label=label,
                            remaining=format_minor_currency(
                                remaining.amount_minor, remaining.exponent, tranche.currency
                            ),
                            original=format_minor_currency(original.amount_minor, original.exponent, tranche.currency),
                            expires_at=tranche.expires_at,
                            scope=None if broad_scope else scope,
                            scope_detail=scope if broad_scope else None,
                            group=org.name,
                        )
                    )
    return balances, grants


def _scope_product(product: str, family: str | None, version: str | None) -> str:
    label = _PRODUCT_LABELS.get(product, product)
    family_label = family.capitalize() if family in {"opus", "sonnet", "haiku"} else family
    qualifiers = ", ".join(value for value in (family_label, version) if value)
    return f"{label} ({qualifiers})" if qualifiers else label


def currently_over_plan(out: ProviderFetch) -> bool:
    """Included quota exhausted with extra usage enabled, without inferring live requests."""
    if not isinstance(out.result, FetchSuccess):
        return False
    extra = out.result.extra_spend
    if extra is None or not extra.is_enabled:
        return False
    return any(is_exhausted(window) for window in out.result.windows if window.display)


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
