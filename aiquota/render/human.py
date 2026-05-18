"""Human-readable CLI rendering — mirrors the GNOME extension popup bars."""

from datetime import datetime

from aiquota.models import AllQuotas, ExtraUsage, ProviderFetch, QuotaWindow
from aiquota.pace import compute_pace
from aiquota.render.format import format_age, format_duration, format_pace, format_pace_forecast
from aiquota.render.view_model import ProviderView, to_view


def render(quotas: AllQuotas) -> str:
    view = to_view(quotas)
    return "\n".join(_render_provider(pv, view.fetched_at) for pv in view.providers)


def _render_provider(pv: ProviderView, now: datetime) -> str:
    out = pv.last_output
    # If the latest call gave us nothing usable, fall back to the prior
    # successful snapshot — stale-but-real numbers beat "no data".
    fallback = pv.last_success if _has_data(pv.last_success) and not _has_data(out) else None
    short, long, stale = _windows_for_render(out, fallback, now)

    if out.error and short is None and long is None:
        return f"{pv.provider}: error — {out.error}"

    if pv.currently_over_plan:
        # Mirror the GNOME popup's collapsed view: while burning, 5h/7d bars are
        # noise — what matters is when the 7d window resets (which ends the burn).
        lines = [f"{pv.provider}  {_format_extra_active(out.extra_usage)}"]
        if out.long_window is not None:
            lines.append(f"  7d reset: ↻ {format_duration(out.long_window.reset_seconds)}")
        return "\n".join(lines)

    lines = [_header(pv)]
    if short is not None:
        lines.append(_window_line("5h", short, stale))
    if long is not None:
        lines.append(_window_line("7d", long, stale))
    if pv.extra_status == "informational" and out.extra_usage is not None:
        # Prepaid still has room, but the user incurred extra-usage spend earlier
        # in the billing month. Surface it so the monthly bill doesn't sneak up.
        lines.append(f"  {_format_extra_informational(out.extra_usage)}")
    if len(lines) == 1:
        lines.append("  no data")
    return "\n".join(lines)


def _has_data(out: ProviderFetch | None) -> bool:
    return out is not None and (out.short_window is not None or out.long_window is not None)


def _windows_for_render(
    out: ProviderFetch, fallback: ProviderFetch | None, now: datetime
) -> tuple[QuotaWindow | None, QuotaWindow | None, str | None]:
    if fallback is None:
        return out.short_window, out.long_window, None
    age = format_age((now - fallback.fetched_at).total_seconds())
    return _refreshed_window(fallback.short_window, now), _refreshed_window(fallback.long_window, now), age


def _refreshed_window(w: QuotaWindow | None, now: datetime) -> QuotaWindow | None:
    # Snapshot's reset_seconds was correct at snapshot time; recompute from
    # reset_at so the countdown shown to the user reflects "now".
    if w is None or w.reset_at is None:
        return w
    return w.model_copy(update={"reset_seconds": max(0.0, (w.reset_at - now).total_seconds())})


def _header(pv: ProviderView) -> str:
    parts = [pv.provider]
    if pv.last_output.error is not None:
        parts.append(f"last refresh failed: {pv.last_output.error}")
    return "  ".join(parts)


def _format_extra_active(extra: ExtraUsage | None) -> str:
    # `⚡` flags "paying above subscription right now" — louder than just a number.
    if extra is None:
        return "⚡ OVER PLAN"
    pct = round(extra.utilization)
    return f"⚡ OVER PLAN — extra ${extra.used_usd:.2f}/${extra.monthly_limit_usd:.0f} ({pct}%) this month"


def _format_extra_informational(extra: ExtraUsage) -> str:
    pct = round(extra.utilization)
    return f"extra: ${extra.used_usd:.2f}/${extra.monthly_limit_usd:.0f} ({pct}%) spent this month"


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
