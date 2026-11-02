"""Human-readable CLI rendering — mirrors the GNOME extension popup bars."""

from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from rich.text import Text

from aiquota.models import (
    AllQuotas,
    AnthropicPlatformCredits,
    AnthropicPlatformScope,
    ExtraSpend,
    FetchSuccess,
    PaidCredits,
    PrepaidBalance,
    PrepaidBalanceAvailable,
)
from aiquota.render.format import (
    format_age,
    format_burn,
    format_known_reset_credit_expiries,
    format_minor_currency,
    format_peak_schedule,
    format_prepaid_balance,
)
from aiquota.render.view_model import AllQuotasView, ProviderView, WindowView, to_view


def render(quotas: AllQuotas, now: datetime | None = None, tz: tzinfo | None = None) -> str:
    """Plain text for snapshots and non-interactive callers."""
    return render_text(quotas, now, tz).plain


def render_text(quotas: AllQuotas, now: datetime | None = None, tz: tzinfo | None = None) -> Text:
    """Apply the server-owned tint to terminal rows; Rich disables color when redirected."""
    render_time = now or datetime.now(UTC)
    return render_view(to_view(quotas, render_time), render_time, tz)


def render_view(view: AllQuotasView, now: datetime | None = None, tz: tzinfo | None = None) -> Text:
    render_time = now or datetime.now(UTC)
    widths = _column_widths(view.providers, render_time)
    return Text("\n").join(_render_provider(provider, render_time, widths, tz) for provider in view.providers)


@dataclass(frozen=True)
class _ColumnWidths:
    reset: int = 0
    pace: int = 0


@dataclass(frozen=True)
class _WindowRow:
    label: str
    used: str
    reset: str
    pace: str | None
    forecast: str | None


def _render_provider(pv: ProviderView, now: datetime, widths: _ColumnWidths, tz: tzinfo | None) -> Text:
    out_result = pv.last_output.result
    error = out_result.error if not isinstance(out_result, FetchSuccess) else None

    (
        windows,
        extra,
        paid_credits,
        reset_credits,
        reset_credit_expiries,
        reset_credit_expiry_lookup_failed,
        stale_age,
    ) = _effective_windows(pv, now)
    credit_line = _format_paid_credits(paid_credits, pv.paid_credits_active)
    prepaid_balance, prepaid_stale_age = _effective_prepaid_balance(pv, now)
    prepaid_balance_line = _format_prepaid_balance(prepaid_balance, prepaid_stale_age)
    platform_credits = pv.display_platform_credits
    platform_lines = []
    if platform_credits:
        platform_lines.append(f"OpenAI API balance · ${platform_credits.total_available_usd:,.2f}")
        for grant in platform_credits.grants:
            expiry = grant.expires_at.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z") if grant.expires_at else "unknown"
            status = " · scheduled" if grant.is_scheduled else " · held" if grant.is_held_for_prepaid_transition else ""
            remaining = grant.granted_usd - grant.used_usd
            platform_lines.append(f"Grant ${remaining:,.2f} / ${grant.granted_usd:,.2f} · expires {expiry}{status}")
    anthropic_lines = _format_anthropic_platform_credits(pv.display_anthropic_platform_credits, tz)

    if error and not windows:
        return Text(
            _header(
                pv.provider,
                error,
                pv.last_output.fetched_at,
                now,
                reset_credits,
                reset_credit_expiries,
                reset_credit_expiry_lookup_failed,
                stale_age,
                tz,
            )
            + (f"  {credit_line}" if credit_line else "")
            + (f"\n  {prepaid_balance_line}" if prepaid_balance_line else "")
            + ("".join(f"\n  {line} (stale)" for line in platform_lines))
            + ("".join(f"\n  {line} (stale)" for line in anthropic_lines))
        )

    if pv.currently_over_plan or pv.paid_credits_active:
        # Mirror the GNOME popup's text-only active-extra view: while burning,
        # bars are noise, but both reset countdowns still matter.
        lines = [
            f"{_provider_label(pv.provider, reset_credits, reset_credit_expiries, tz, reset_credit_expiry_lookup_failed)}  "
            f"{credit_line if pv.paid_credits_active else _format_extra_active(extra)}"
        ]
        if prepaid_balance_line:
            lines.append(f"  {prepaid_balance_line}")
        lines.append(_active_windows_line(windows))
        lines.extend(_burn_lines(pv, now, tz))
        return Text("\n".join(lines))

    lines = [
        _header(
            pv.provider,
            error,
            pv.last_output.fetched_at,
            now,
            reset_credits,
            reset_credit_expiries,
            reset_credit_expiry_lookup_failed,
            stale_age,
            tz,
        )
    ]
    if credit_line:
        lines.append(f"  {credit_line}")
    if prepaid_balance_line:
        lines.append(f"  {prepaid_balance_line}")
    lines.extend(f"  {line}" for line in platform_lines)
    lines.extend(f"  {line}" for line in anthropic_lines)
    lines.extend(_burn_lines(pv, now, tz))
    parts = [Text(line) for line in lines]
    colors = {"cool": "blue", "ok": "white", "warn": "yellow", "hot": "red", "unknown": "white", "stale": "yellow"}
    parts.extend(
        Text(_format_window_line(_window_row(window), widths), style=colors["stale" if stale_age else window.tint])
        for window in windows
    )
    # Prepaid still has room, but the user incurred billable spend earlier in the
    # billing month. Surface it so the monthly bill doesn't sneak up. Built as a
    # value rather than a flag so `extra`'s narrowing survives into the call.
    extra_line = (
        f"  {_format_extra_informational(extra)}" if pv.extra_status == "informational" and extra is not None else None
    )
    if extra_line is not None:
        parts.append(Text(extra_line))
    if (
        not windows
        and extra_line is None
        and prepaid_balance_line is None
        and not platform_lines
        and not anthropic_lines
    ):
        # Gate on "nothing substantive was reported", not on line count — the
        # burn schedule adds lines without saying anything about quota.
        parts.append(Text("  no data"))
    return Text("\n").join(parts)


def _effective_prepaid_balance(pv: ProviderView, now: datetime) -> tuple[PrepaidBalance | None, str | None]:
    stale_since = pv.display.prepaid_stale_since
    age = format_age((now - stale_since).total_seconds()) if stale_since is not None else None
    return pv.display.prepaid_balance, age


def _burn_lines(pv: ProviderView, now: datetime, tz: tzinfo | None) -> list[str]:
    if pv.burn is None:
        return []
    lines = [f"  {format_burn(pv.burn, now, tz)}"]
    schedule = format_peak_schedule(pv.burn, tz)
    if schedule:
        lines.append(f"    {schedule}")
    return lines


def _effective_windows(
    pv: ProviderView, now: datetime
) -> tuple[list[WindowView], ExtraSpend | None, PaidCredits | None, int | None, list[datetime], bool, str | None]:
    result = pv.display.result
    stale_since = pv.display.observed_at if pv.display.source == "last_success" else None
    stale_age = format_age((now - stale_since).total_seconds()) if stale_since is not None else None
    if result is None:
        return [], None, None, None, [], False, None
    return (
        pv.display_windows,
        result.extra_spend,
        result.paid_credits,
        result.available_reset_credits,
        result.available_reset_credit_expiries,
        result.reset_credit_expiry_lookup_failed,
        stale_age,
    )


def _column_widths(providers: list[ProviderView], now: datetime) -> _ColumnWidths:
    rows: list[_WindowRow] = []
    for pv in providers:
        if pv.currently_over_plan or pv.paid_credits_active:
            continue
        windows, _, _, _, _, _, _ = _effective_windows(pv, now)
        rows.extend(_window_row(window) for window in windows)
    return _ColumnWidths(
        reset=max((len(row.reset) for row in rows), default=0),
        pace=max((len(row.pace) for row in rows if row.pace), default=0),
    )


def _header(
    provider: str,
    error: str | None,
    checked_at: datetime,
    now: datetime,
    reset_credits: int | None,
    reset_credit_expiries: list[datetime],
    reset_credit_expiry_lookup_failed: bool,
    stale_age: str | None,
    tz: tzinfo | None,
) -> str:
    parts = [_provider_label(provider, reset_credits, reset_credit_expiries, tz, reset_credit_expiry_lookup_failed)]
    if error is None:
        if stale_age is not None:
            parts.append(f"(stale {stale_age})")
        return "  ".join(parts)
    checked_age = format_age((now - checked_at).total_seconds())
    prefix = "last refresh failed" if stale_age is not None else "check failed"
    parts.append(f"{prefix} {checked_age} ago: {error}")
    if stale_age is not None:
        parts.append(f"(stale {stale_age})")
    return "  ".join(parts)


def _provider_label(
    provider: str,
    reset_credits: int | None,
    reset_credit_expiries: list[datetime],
    tz: tzinfo | None,
    reset_credit_expiry_lookup_failed: bool,
) -> str:
    if provider == "openai_platform":
        provider = "OpenAI API"
    if provider == "anthropic_platform":
        provider = "Anthropic API"
    if reset_credits is None:
        return provider
    noun = "reset" if reset_credits == 1 else "resets"
    label = f"{provider} · {reset_credits} banked {noun}"
    if reset_credit_expiry_lookup_failed:
        return f"{label} · expiry dates unavailable"
    if expiries := format_known_reset_credit_expiries(reset_credit_expiries, tz):
        return f"{label} · known expiries: {expiries}"
    return label


def _format_anthropic_platform_credits(credits: AnthropicPlatformCredits | None, tz: tzinfo | None) -> list[str]:
    if credits is None:
        return []
    lines: list[str] = []
    for organization in credits.organizations:
        if organization.error is not None:
            lines.append(f"{organization.name} · billing lookup failed: {organization.error}")
            continue
        balance = organization.balance.credits if organization.balance is not None else None
        if balance is None:
            lines.append(f"{organization.name} · balance unavailable")
        else:
            currency = organization.currency or "USD"
            amount = format_minor_currency(balance.amount_minor, balance.exponent, currency)
            lines.append(f"{organization.name} balance · {amount}")
        for label, tranches in (("Grant", organization.tranches), ("Promo grant", organization.promo_tranches)):
            for tranche in tranches:
                granted = tranche.granted.credits
                remaining = tranche.remaining.credits
                if granted is None or remaining is None:
                    continue
                original = format_minor_currency(granted.amount_minor, granted.exponent, tranche.currency)
                available = format_minor_currency(remaining.amount_minor, remaining.exponent, tranche.currency)
                expiry = (
                    tranche.expires_at.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z") if tranche.expires_at else "unknown"
                )
                scope = _anthropic_scope_label(tranche.scope)
                suffix = f" · {scope}" if scope else ""
                lines.append(f"{label} {available} / {original} · expires {expiry}{suffix}")
    return lines


def _anthropic_scope_label(scope: AnthropicPlatformScope) -> str | None:
    if scope.kind in {"unrestricted", "unscoped"} and not scope.applies_to:
        return None
    products = sorted(
        {
            f"{item.product} ({', '.join(value for value in (item.model_family, item.model_version) if value)})"
            if item.model_family or item.model_version
            else item.product
            for item in scope.applies_to
        }
    )
    return f"scope: {', '.join(products)}" if products else f"scope: {scope.kind}"


def _format_extra_active(extra: ExtraSpend | None) -> str:
    # `⚡` flags "paying above subscription right now" — louder than just a number.
    if extra is None:
        return "⚡ OVER PLAN"
    pct = round(extra.utilization)
    return f"⚡ OVER PLAN — extra ${extra.used_usd:.2f}/${extra.monthly_limit_usd:.0f} ({pct}%) this month"


def _format_extra_informational(extra: ExtraSpend) -> str:
    pct = round(extra.utilization)
    return f"extra: ${extra.used_usd:.2f}/${extra.monthly_limit_usd:.0f} ({pct}%) spent this month"


def _format_paid_credits(credits: PaidCredits | None, active: bool) -> str | None:
    if credits is None:
        return None
    if credits.unlimited:
        balance = "unlimited credits"
    elif credits.balance is None:
        balance = "credits available"
    else:
        try:
            amount = Decimal(credits.balance)
        except InvalidOperation:
            return "credits available"
        if not amount.is_finite():
            return "credits available"
        balance = f"{amount.quantize(Decimal(1), rounding=ROUND_HALF_UP):,} credits left"
    return f"⚡ using paid credits · {balance}" if active else balance


def _format_prepaid_balance(balance: PrepaidBalance | None, stale_age: str | None = None) -> str | None:
    if balance is None:
        return None
    if not isinstance(balance, PrepaidBalanceAvailable):
        line = "Claude prepaid balance unavailable"
    else:
        line = f"Claude prepaid balance · {format_prepaid_balance(balance)}"
    if stale_age is not None:
        line += f" (stale {stale_age})"
    return line


def _active_windows_line(windows: list[WindowView]) -> str:
    return "  " + "  ".join(_active_window_part(window) for window in windows)


def _active_window_part(window: WindowView) -> str:
    return f"{window.label}: {window.used:>3d}% ↻ {window.reset}"


def _window_row(w: WindowView) -> _WindowRow:
    return _WindowRow(
        label=w.label, used=f"{w.used:>3d}%", reset=w.reset, pace=f"Δ{w.pace}" if w.pace else None, forecast=w.forecast
    )


def _format_window_line(row: _WindowRow, widths: _ColumnWidths) -> str:
    parts = [f"{row.label}: {row.used}", f"↻ {row.reset:<{widths.reset}}"]
    if row.pace:
        parts.append(f"{row.pace:<{widths.pace}}" if row.forecast else row.pace)
    if row.forecast:
        parts.append(row.forecast)
    return ("  " + "  ".join(parts)).rstrip()
