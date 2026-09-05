"""Human-readable CLI rendering — mirrors the GNOME extension popup bars."""

from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo

from aiquota.models import AllQuotas, ExtraSpend, FetchSuccess, QuotaWindow, SuccessfulProviderFetch
from aiquota.pace import compute_pace, is_exhausted
from aiquota.render.format import (
    format_age,
    format_burn,
    format_duration,
    format_known_reset_credit_expiries,
    format_pace,
    format_pace_forecast,
    format_peak_schedule,
    format_window_label,
)
from aiquota.render.view_model import ProviderView, to_view


def render(quotas: AllQuotas, now: datetime | None = None, tz: tzinfo | None = None) -> str:
    """`tz` defaults to system local; tests pin it so snapshots hold on any machine."""
    render_time = now or datetime.now(UTC)
    view = to_view(quotas, render_time)
    widths = _column_widths(view.providers, render_time)
    return "\n".join(_render_provider(pv, render_time, widths, tz) for pv in view.providers)


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


def _render_provider(pv: ProviderView, now: datetime, widths: _ColumnWidths, tz: tzinfo | None) -> str:
    out_result = pv.last_output.result
    error = out_result.error if not isinstance(out_result, FetchSuccess) else None

    windows, extra, reset_credits, reset_credit_expiries, stale_age = _effective_windows(pv, now)

    if error and not windows:
        return _header(
            pv.provider, error, pv.last_output.fetched_at, now, reset_credits, reset_credit_expiries, stale_age, tz
        )

    if pv.currently_over_plan:
        # Mirror the GNOME popup's text-only active-extra view: while burning,
        # bars are noise, but both reset countdowns still matter.
        lines = [
            f"{_provider_label(pv.provider, reset_credits, reset_credit_expiries, tz)}  {_format_extra_active(extra)}"
        ]
        lines.append(_active_windows_line(windows))
        lines.extend(_burn_lines(pv, now, tz))
        return "\n".join(lines)

    lines = [
        _header(pv.provider, error, pv.last_output.fetched_at, now, reset_credits, reset_credit_expiries, stale_age, tz)
    ]
    lines.extend(_burn_lines(pv, now, tz))
    lines.extend(_format_window_line(_window_row(window), widths) for window in windows)
    # Prepaid still has room, but the user incurred billable spend earlier in the
    # billing month. Surface it so the monthly bill doesn't sneak up. Built as a
    # value rather than a flag so `extra`'s narrowing survives into the call.
    extra_line = (
        f"  {_format_extra_informational(extra)}" if pv.extra_status == "informational" and extra is not None else None
    )
    if extra_line is not None:
        lines.append(extra_line)
    if not windows and extra_line is None:
        # Gate on "nothing substantive was reported", not on line count — the
        # burn schedule adds lines without saying anything about quota.
        lines.append("  no data")
    return "\n".join(lines)


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
) -> tuple[list[QuotaWindow], ExtraSpend | None, int | None, list[datetime], str | None]:
    out_result = pv.last_output.result
    # If the latest call gave us nothing usable, fall back to the prior
    # successful snapshot — stale-but-real numbers beat "no data".
    if isinstance(out_result, FetchSuccess) and (out_result.windows or out_result.available_reset_credits is not None):
        return (
            [window for window in out_result.windows if window.display],
            out_result.extra_spend,
            out_result.available_reset_credits,
            out_result.available_reset_credit_expiries,
            None,
        )
    if pv.last_success is not None:
        return _stale_windows(pv.last_success, now)
    return [], None, None, [], None


def _column_widths(providers: list[ProviderView], now: datetime) -> _ColumnWidths:
    rows: list[_WindowRow] = []
    for pv in providers:
        if pv.currently_over_plan:
            continue
        windows, _, _, _, _ = _effective_windows(pv, now)
        rows.extend(_window_row(window) for window in windows)
    return _ColumnWidths(
        reset=max((len(row.reset) for row in rows), default=0),
        pace=max((len(row.pace) for row in rows if row.pace), default=0),
    )


def _stale_windows(
    snap: SuccessfulProviderFetch, now: datetime
) -> tuple[list[QuotaWindow], ExtraSpend | None, int | None, list[datetime], str]:
    return (
        [_refreshed_window(window, now) for window in snap.result.windows if window.display],
        snap.result.extra_spend,
        snap.result.available_reset_credits,
        snap.result.available_reset_credit_expiries,
        format_age((now - snap.fetched_at).total_seconds()),
    )


def _refreshed_window(w: QuotaWindow, now: datetime) -> QuotaWindow:
    # Snapshot's reset_seconds was correct at snapshot time; recompute from
    # reset_at so the countdown shown to the user reflects "now".
    if w.reset_at is None:
        return w
    return w.model_copy(update={"reset_seconds": max(0.0, (w.reset_at - now).total_seconds())})


def _header(
    provider: str,
    error: str | None,
    checked_at: datetime,
    now: datetime,
    reset_credits: int | None,
    reset_credit_expiries: list[datetime],
    stale_age: str | None,
    tz: tzinfo | None,
) -> str:
    parts = [_provider_label(provider, reset_credits, reset_credit_expiries, tz)]
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
    provider: str, reset_credits: int | None, reset_credit_expiries: list[datetime], tz: tzinfo | None
) -> str:
    if reset_credits is None:
        return provider
    noun = "reset" if reset_credits == 1 else "resets"
    label = f"{provider} · {reset_credits} banked {noun}"
    if expiries := format_known_reset_credit_expiries(reset_credit_expiries, tz):
        return f"{label} · known expiries: {expiries}"
    return label


def _format_extra_active(extra: ExtraSpend | None) -> str:
    # `⚡` flags "paying above subscription right now" — louder than just a number.
    if extra is None:
        return "⚡ OVER PLAN"
    pct = round(extra.utilization)
    return f"⚡ OVER PLAN — extra ${extra.used_usd:.2f}/${extra.monthly_limit_usd:.0f} ({pct}%) this month"


def _format_extra_informational(extra: ExtraSpend) -> str:
    pct = round(extra.utilization)
    return f"extra: ${extra.used_usd:.2f}/${extra.monthly_limit_usd:.0f} ({pct}%) spent this month"


def _active_windows_line(windows: list[QuotaWindow]) -> str:
    return "  " + "  ".join(_active_window_part(window) for window in windows)


def _active_window_part(window: QuotaWindow) -> str:
    label = format_window_label(window)
    return f"{label}: {_display_used_percent(window):>3d}% ↻ {format_duration(window.reset_seconds)}"


def _display_used_percent(w: QuotaWindow) -> int:
    rounded = round(w.used_percent)
    return rounded if is_exhausted(w) else min(rounded, 99)


def _window_row(w: QuotaWindow) -> _WindowRow:
    used = f"{_display_used_percent(w):>3d}%"
    if is_exhausted(w):
        return _WindowRow(
            label=format_window_label(w),
            used=used,
            reset=format_duration(w.reset_seconds),
            pace=None,
            forecast="exhausted",
        )
    pace = compute_pace(w)
    pace_str = format_pace(pace)
    return _WindowRow(
        label=format_window_label(w),
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
