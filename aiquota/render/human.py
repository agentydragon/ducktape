"""Human-readable CLI rendering — mirrors the GNOME extension popup bars."""

from datetime import UTC, datetime

from aiquota.models import AllQuotas, ExtraUsage, FetchSuccess, QuotaWindow, SuccessfulProviderFetch
from aiquota.pace import compute_pace
from aiquota.render.format import format_age, format_duration, format_pace, format_pace_forecast
from aiquota.render.view_model import ProviderView, to_view


def render(quotas: AllQuotas, now: datetime | None = None) -> str:
    view = to_view(quotas)
    render_time = now or datetime.now(UTC)
    return "\n".join(_render_provider(pv, render_time) for pv in view.providers)


def _render_provider(pv: ProviderView, now: datetime) -> str:
    out_result = pv.last_output.result
    error = out_result.error if not isinstance(out_result, FetchSuccess) else None

    # If the latest call gave us nothing usable, fall back to the prior
    # successful snapshot — stale-but-real numbers beat "no data".
    if isinstance(out_result, FetchSuccess) and (out_result.short_window or out_result.long_window):
        short, long, extra, stale = out_result.short_window, out_result.long_window, out_result.extra_usage, None
    elif pv.last_success is not None:
        short, long, extra, stale = _stale_windows(pv.last_success, now)
    else:
        short = long = extra = None
        stale = None

    if error and short is None and long is None:
        return _header(pv.provider, error, pv.last_output.fetched_at, now)

    if pv.currently_over_plan:
        # Mirror the GNOME popup's text-only active-extra view: while burning,
        # bars are noise, but both reset countdowns still matter.
        lines = [f"{pv.provider}  {_format_extra_active(extra)}"]
        lines.append(_active_windows_line(short, long, stale))
        return "\n".join(lines)

    lines = [_header(pv.provider, error, pv.last_output.fetched_at, now)]
    if short is not None:
        lines.append(_window_line("5h", short, stale))
    if long is not None:
        lines.append(_window_line("7d", long, stale))
    if pv.extra_status == "informational" and extra is not None:
        # Prepaid still has room, but the user incurred extra-usage spend earlier
        # in the billing month. Surface it so the monthly bill doesn't sneak up.
        lines.append(f"  {_format_extra_informational(extra)}")
    if len(lines) == 1:
        lines.append("  no data")
    return "\n".join(lines)


def _stale_windows(
    snap: SuccessfulProviderFetch, now: datetime
) -> tuple[QuotaWindow | None, QuotaWindow | None, ExtraUsage | None, str]:
    return (
        _refreshed_window(snap.result.short_window, now),
        _refreshed_window(snap.result.long_window, now),
        snap.result.extra_usage,
        format_age((now - snap.fetched_at).total_seconds()),
    )


def _refreshed_window(w: QuotaWindow | None, now: datetime) -> QuotaWindow | None:
    # Snapshot's reset_seconds was correct at snapshot time; recompute from
    # reset_at so the countdown shown to the user reflects "now".
    if w is None or w.reset_at is None:
        return w
    return w.model_copy(update={"reset_seconds": max(0.0, (w.reset_at - now).total_seconds())})


def _header(provider: str, error: str | None, checked_at: datetime, now: datetime) -> str:
    if error is None:
        return provider
    checked_age = format_age((now - checked_at).total_seconds())
    return f"{provider}  check failed {checked_age} ago: {error}"


def _format_extra_active(extra: ExtraUsage | None) -> str:
    # `⚡` flags "paying above subscription right now" — louder than just a number.
    if extra is None:
        return "⚡ OVER PLAN"
    pct = round(extra.utilization)
    return f"⚡ OVER PLAN — extra ${extra.used_usd:.2f}/${extra.monthly_limit_usd:.0f} ({pct}%) this month"


def _format_extra_informational(extra: ExtraUsage) -> str:
    pct = round(extra.utilization)
    return f"extra: ${extra.used_usd:.2f}/${extra.monthly_limit_usd:.0f} ({pct}%) spent this month"


def _active_windows_line(short: QuotaWindow | None, long: QuotaWindow | None, stale_age: str | None) -> str:
    parts = [_active_window_part("5h", short), _active_window_part("7d", long)]
    if stale_age is not None:
        parts.append(f"(stale {stale_age})")
    return "  " + "  ".join(parts)


def _active_window_part(label: str, w: QuotaWindow | None) -> str:
    if w is None:
        return f"{label}: no data"
    return f"{label}: {round(w.used_percent):>3d}% ↻ {format_duration(w.reset_seconds)}"


def _window_line(label: str, w: QuotaWindow, stale_age: str | None) -> str:
    used = f"{round(w.used_percent):>3d}%"
    reset = f"↻ {format_duration(w.reset_seconds)}"
    pace = compute_pace(w)
    parts = [f"{label}: {used}", reset]
    pace_str = format_pace(pace)
    if pace_str:
        parts.append(f"Δ{pace_str}")
    forecast = format_pace_forecast(pace, w.reset_seconds)
    if forecast:
        parts.append(forecast)
    if stale_age is not None:
        parts.append(f"(stale {stale_age})")
    return "  " + "  ".join(parts)
