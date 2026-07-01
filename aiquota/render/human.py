"""Human-readable CLI rendering — mirrors the GNOME extension popup bars."""

from dataclasses import dataclass
from datetime import UTC, datetime

from aiquota.models import AllQuotas, ExtraSpend, FetchSuccess, QuotaWindow, SuccessfulProviderFetch
from aiquota.pace import compute_pace
from aiquota.render.format import format_age, format_duration, format_pace, format_pace_forecast
from aiquota.render.view_model import ProviderView, to_view


def render(quotas: AllQuotas, now: datetime | None = None) -> str:
    view = to_view(quotas)
    render_time = now or datetime.now(UTC)
    widths = _column_widths(view.providers, render_time)
    return "\n".join(_render_provider(pv, render_time, widths) for pv in view.providers)


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


def _render_provider(pv: ProviderView, now: datetime, widths: _ColumnWidths) -> str:
    out_result = pv.last_output.result
    error = out_result.error if not isinstance(out_result, FetchSuccess) else None

    short, long, extra, stale_age = _effective_windows(pv, now)

    if error and short is None and long is None:
        return _header(pv.provider, error, pv.last_output.fetched_at, now, stale_age)

    if pv.currently_over_plan:
        # Mirror the GNOME popup's text-only active-extra view: while burning,
        # bars are noise, but both reset countdowns still matter.
        lines = [f"{pv.provider}  {_format_extra_active(extra)}"]
        lines.append(_active_windows_line(short, long))
        return "\n".join(lines)

    lines = [_header(pv.provider, error, pv.last_output.fetched_at, now, stale_age)]
    if short is not None:
        lines.append(_format_window_line(_window_row("5h", short), widths))
    if long is not None:
        lines.append(_format_window_line(_window_row("7d", long), widths))
    if pv.extra_status == "informational" and extra is not None:
        # Prepaid still has room, but the user incurred billable spend earlier
        # in the billing month. Surface it so the monthly bill doesn't sneak up.
        lines.append(f"  {_format_extra_informational(extra)}")
    if len(lines) == 1:
        lines.append("  no data")
    return "\n".join(lines)


def _effective_windows(
    pv: ProviderView, now: datetime
) -> tuple[QuotaWindow | None, QuotaWindow | None, ExtraSpend | None, str | None]:
    out_result = pv.last_output.result
    # If the latest call gave us nothing usable, fall back to the prior
    # successful snapshot — stale-but-real numbers beat "no data".
    if isinstance(out_result, FetchSuccess) and (out_result.short_window or out_result.long_window):
        return out_result.short_window, out_result.long_window, out_result.extra_spend, None
    if pv.last_success is not None:
        return _stale_windows(pv.last_success, now)
    return None, None, None, None


def _column_widths(providers: list[ProviderView], now: datetime) -> _ColumnWidths:
    rows: list[_WindowRow] = []
    for pv in providers:
        if pv.currently_over_plan:
            continue
        short, long, _, _ = _effective_windows(pv, now)
        if short is not None:
            rows.append(_window_row("5h", short))
        if long is not None:
            rows.append(_window_row("7d", long))
    return _ColumnWidths(
        reset=max((len(row.reset) for row in rows), default=0),
        pace=max((len(row.pace) for row in rows if row.pace), default=0),
    )


def _stale_windows(
    snap: SuccessfulProviderFetch, now: datetime
) -> tuple[QuotaWindow | None, QuotaWindow | None, ExtraSpend | None, str]:
    return (
        _refreshed_window(snap.result.short_window, now),
        _refreshed_window(snap.result.long_window, now),
        snap.result.extra_spend,
        format_age((now - snap.fetched_at).total_seconds()),
    )


def _refreshed_window(w: QuotaWindow | None, now: datetime) -> QuotaWindow | None:
    # Snapshot's reset_seconds was correct at snapshot time; recompute from
    # reset_at so the countdown shown to the user reflects "now".
    if w is None or w.reset_at is None:
        return w
    return w.model_copy(update={"reset_seconds": max(0.0, (w.reset_at - now).total_seconds())})


def _header(provider: str, error: str | None, checked_at: datetime, now: datetime, stale_age: str | None) -> str:
    parts = [provider]
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


def _format_extra_active(extra: ExtraSpend | None) -> str:
    # `⚡` flags "paying above subscription right now" — louder than just a number.
    if extra is None:
        return "⚡ OVER PLAN"
    pct = round(extra.utilization)
    return f"⚡ OVER PLAN — extra ${extra.used_usd:.2f}/${extra.monthly_limit_usd:.0f} ({pct}%) this month"


def _format_extra_informational(extra: ExtraSpend) -> str:
    pct = round(extra.utilization)
    return f"extra: ${extra.used_usd:.2f}/${extra.monthly_limit_usd:.0f} ({pct}%) spent this month"


def _active_windows_line(short: QuotaWindow | None, long: QuotaWindow | None) -> str:
    parts = [_active_window_part("5h", short), _active_window_part("7d", long)]
    return "  " + "  ".join(parts)


def _active_window_part(label: str, w: QuotaWindow | None) -> str:
    if w is None:
        return f"{label}: no data"
    return f"{label}: {round(w.used_percent):>3d}% ↻ {format_duration(w.reset_seconds)}"


def _window_row(label: str, w: QuotaWindow) -> _WindowRow:
    used = f"{round(w.used_percent):>3d}%"
    pace = compute_pace(w)
    pace_str = format_pace(pace)
    return _WindowRow(
        label=label,
        used=used,
        reset=format_duration(w.reset_seconds),
        pace=f"Δ{pace_str}" if pace_str else None,
        forecast=format_pace_forecast(pace, w.reset_seconds),
    )


def _format_window_line(row: _WindowRow, widths: _ColumnWidths) -> str:
    parts = [f"{row.label}: {row.used}", f"↻ {row.reset:<{widths.reset}}"]
    if row.pace:
        parts.append(f"{row.pace:<{widths.pace}}" if row.forecast else row.pace)
    if row.forecast:
        parts.append(row.forecast)
    return ("  " + "  ".join(parts)).rstrip()
