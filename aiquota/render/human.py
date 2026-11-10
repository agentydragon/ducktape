"""Human-readable CLI rendering for subscription and API credit data."""

from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from decimal import Decimal

from rich.text import Text

from aiquota.models import AllQuotas
from aiquota.render.format import (
    format_age,
    format_burn,
    format_deadline,
    format_peak_schedule,
    format_reset_credit_summary,
)
from aiquota.render.view_model import (
    AllQuotasView,
    BalanceView,
    GrantView,
    ProviderView,
    WindowView,
    show_reset_credits,
    to_view,
)

_API_PROVIDERS = {"openai_platform", "anthropic_platform"}
_TINTS = {"cool": "blue", "ok": "white", "warn": "yellow", "hot": "red", "unknown": "white", "stale": "yellow"}


def render(quotas: AllQuotas, now: datetime | None = None, tz: tzinfo | None = None, *, details: bool = False) -> str:
    """Plain text for snapshots and non-interactive callers."""
    return render_text(quotas, now, tz, details=details).plain


def render_text(
    quotas: AllQuotas, now: datetime | None = None, tz: tzinfo | None = None, *, details: bool = False
) -> Text:
    """Apply shared view tints to terminal rows; Rich disables color when redirected."""
    render_time = now or datetime.now(UTC)
    return render_view(to_view(quotas, render_time), render_time, tz, details=details)


def render_view(
    view: AllQuotasView, now: datetime | None = None, tz: tzinfo | None = None, *, details: bool = False
) -> Text:
    render_time = now or view.presentation_at or datetime.now(UTC)
    providers = sorted(view.providers, key=lambda provider: provider.provider in _API_PROVIDERS)
    widths = _column_widths(providers)
    subscriptions = [provider for provider in providers if provider.provider not in _API_PROVIDERS]
    api_balances = [provider for provider in providers if provider.provider in _API_PROVIDERS]
    blocks = []
    if subscriptions and api_balances:
        for heading, group in (("Subscriptions", subscriptions), ("API balances", api_balances)):
            section = Text(f"{heading}\n\n")
            section.append(_render_providers(group, render_time, tz, details, widths))
            blocks.append(section)
    else:
        blocks.append(_render_providers(providers, render_time, tz, details, widths))
    if view.connection_error is not None:
        if view.providers:
            age = format_age((render_time - view.fetched_at).total_seconds())
            offline = Text(f"Offline · cached snapshot from {age} ago")
        else:
            offline = Text("Offline · no cached observations")
        if details:
            offline.append(f"\n  connection error: {view.connection_error}")
        blocks.insert(0, offline)
    return Text("\n\n").join(blocks)


@dataclass(frozen=True)
class _ColumnWidths:
    label: int
    reset: int


def _render_providers(
    providers: list[ProviderView], now: datetime, tz: tzinfo | None, details: bool, widths: _ColumnWidths
) -> Text:
    return Text("\n\n").join(_render_provider(provider, now, tz, details, widths) for provider in providers)


def _column_widths(providers: list[ProviderView]) -> _ColumnWidths:
    windows = [window for provider in providers for window in provider.display_windows]
    return _ColumnWidths(
        label=max((len(window.label) for window in windows), default=0),
        reset=max((len(window.reset) for window in windows), default=0),
    )


def _render_provider(pv: ProviderView, now: datetime, tz: tzinfo | None, details: bool, widths: _ColumnWidths) -> Text:
    lines = [Text(pv.label or pv.provider)]
    result = pv.display.result
    substantive = bool(
        pv.display_windows
        or pv.balances
        or pv.grants
        or (result is not None and (result.available_reset_credits or 0) > 0)
    )

    if pv.stale_since is not None:
        age = format_age((now - pv.stale_since).total_seconds())
        lines.append(Text(f"  Stale data · last observed {age} ago", style="yellow"))
    if pv.error_summary is not None:
        error_line = f"  {pv.error_summary}"
        if substantive:
            error_line += " · showing available data"
        lines.append(Text(error_line))
        if details and pv.error_detail is not None:
            lines.append(Text(f"    error: {pv.error_detail}"))

    lines.extend(_credit_lines(pv, now, tz, details))
    if pv.status is not None:
        lines.append(Text(f"  {pv.status}"))

    if pv.burn is not None and substantive:
        lines.append(Text(f"  {format_burn(pv.burn, now, tz)}"))
        if details:
            schedule = format_peak_schedule(pv.burn, tz)
            if schedule:
                lines.append(Text(f"    {schedule}"))

    if (
        result is not None
        and result.available_reset_credits is not None
        and show_reset_credits(result.available_reset_credits, pv.display_windows, details=details)
    ):
        summary = format_reset_credit_summary(
            result.available_reset_credits,
            result.available_reset_credit_expiries,
            result.reset_credit_expiry_lookup_failed,
            now,
            tz,
            details=details,
        )
        lines.append(Text(f"  Banked resets: {summary}"))

    if pv.display_windows:
        lines.extend(
            Text(_format_window_line(window, widths, details), style=_TINTS[window.tint])
            for window in pv.display_windows
        )
    elif not substantive and pv.error_summary is None:
        lines.append(Text("  No quota or credit data available"))

    extra = result.extra_spend if result is not None else None
    if extra is not None and extra.used_usd > 0:
        lines.append(
            Text(
                f"  Extra spend this month: {_usd(extra.used_usd)} of {_usd(extra.monthly_limit_usd, whole=True)} limit"
            )
        )

    return Text("\n").join(lines)


def _credit_lines(pv: ProviderView, now: datetime, tz: tzinfo | None, details: bool) -> list[Text]:
    groups = list(dict.fromkeys([row.group for row in pv.balances] + [row.group for row in pv.grants]))
    lines: list[Text] = []
    for group in groups:
        if group is not None:
            lines.append(Text(f"  {group}"))
        indent = "    " if group is not None else "  "
        lines.extend(_balance_line(balance, indent, now, details) for balance in pv.balances if balance.group == group)
        lines.extend(_grant_line(grant, indent, now, tz, details) for grant in pv.grants if grant.group == group)
    return lines


def _balance_line(balance: BalanceView, indent: str, now: datetime, details: bool) -> Text:
    stale = f" · stale {format_age((now - balance.stale_since).total_seconds())}" if balance.stale_since else ""
    line = Text(f"{indent}{balance.label}: {balance.value}{stale}")
    if details and balance.error is not None:
        line.append(f"\n{indent}  error: {balance.error}")
    return line


def _grant_line(grant: GrantView, indent: str, now: datetime, tz: tzinfo | None, details: bool) -> Text:
    line = f"{indent}{grant.label}: {grant.remaining} remaining"
    if grant.expires_at is None:
        line += " · expiry unknown"
    else:
        line += f" · expires {format_deadline(grant.expires_at, now, tz, details=details)}"
    scope = (grant.scope_detail or grant.scope) if details else grant.scope
    if scope:
        line += f" · scope: {scope}"
    if grant.status:
        line += f" · {grant.status}"
    if details:
        line += f" · {grant.original} granted"
    return Text(line)


def _usd(amount: float, *, whole: bool = False) -> str:
    precision = ",.0f" if whole else ",.2f"
    return f"${format(Decimal(str(amount)), precision)}"


def _format_window_line(window: WindowView, widths: _ColumnWidths, details: bool) -> str:
    line = f"  {window.label:<{widths.label}}: {window.used:>3}% used  ↻ {window.reset:<{widths.reset}}"
    if window.forecast:
        line += f"  {window.forecast}"
    if details and window.pace:
        line += f"  pace {window.pace}"
    return line.rstrip()
