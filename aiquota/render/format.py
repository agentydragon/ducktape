"""Shared format helpers for human and tmux renderers."""

from datetime import datetime, tzinfo

from aiquota.models import PaceResult, QuotaWindow
from aiquota.peak_windows import BurnStatus, PeakInterval


def format_duration(seconds: float) -> str:
    s = max(0, round(seconds))
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d > 0:
        return f"{d}d{h}h"
    if h > 0:
        return f"{h}h{m:02d}m"
    return f"{m}m"


def format_window_duration(seconds: float) -> str:
    rounded = round(seconds)
    if rounded % 86400 == 0:
        return f"{rounded // 86400}d"
    if rounded % 3600 == 0:
        return f"{rounded // 3600}h"
    if rounded % 60 == 0:
        return f"{rounded // 60}m"
    return f"{rounded}s"


def format_window_label(window: QuotaWindow) -> str:
    duration = format_window_duration(window.window_seconds)
    return f"{window.name} ({duration})" if window.name else duration


def format_age(seconds: float) -> str:
    s = max(0, round(seconds))
    if s < 60:
        return f"{s}s"
    return format_duration(s)


def format_known_reset_credit_expiries(expiries: list[datetime], tz: tzinfo | None = None) -> str | None:
    """Render the detail endpoint's known expiries in the viewer's local time.

    The endpoint can return fewer credit rows than the authoritative count, so
    callers must retain the "known" qualifier rather than imply this is a
    complete list.
    """

    if not expiries:
        return None
    return ", ".join(expiry.astimezone(tz).strftime("%b %-d %H:%M") for expiry in expiries)


def format_pace(pace: PaceResult | None) -> str | None:
    if pace is None or not pace.stable:
        return None
    sign = "+" if pace.deviation >= 0 else "-"
    return f"{sign}{abs(round(pace.deviation))}%"


def format_pace_forecast(pace: PaceResult | None, reset_seconds: float) -> str | None:
    if pace is None or not pace.stable or pace.projected_at_reset is None:
        return None
    projected = pace.projected_at_reset
    if projected > 100.5 and pace.seconds_to_exhaust is not None:
        shortfall = reset_seconds - pace.seconds_to_exhaust
        return f"exhausts ~{format_duration(shortfall)} before reset"
    if projected < 95:
        return f"leaves ~{round(100 - projected)}% unused at reset"
    return "on pace"


def format_local_clock(when: datetime, tz: tzinfo | None = None) -> str:
    """Render an instant as wall-clock time in `tz`, defaulting to system local.

    Vendors publish peak hours in their own zone and usually omit it; showing
    the viewer's clock is the whole point of surfacing this. The parameter
    exists so snapshot tests can pin a zone — without it, rendered output
    differs per machine and the snapshots only hold where they were recorded.
    """
    return when.astimezone(tz).strftime("%H:%M")


def format_multiplier(value: float) -> str:
    return f"{value:g}x"


def format_peak_interval(interval: PeakInterval, tz: tzinfo | None = None) -> str:
    """One upcoming window as `Day HH:MM-HH:MM` in `tz` (default system local)."""
    start = interval.start.astimezone(tz)
    return f"{start:%a} {start:%H:%M}-{format_local_clock(interval.end, tz)}"


def format_burn(burn: BurnStatus, now: datetime, tz: tzinfo | None = None) -> str:
    until = format_duration((burn.changes_at - now).total_seconds())
    if burn.in_peak:
        clock = format_local_clock(burn.changes_at, tz)
        return f"🔥 {format_multiplier(burn.multiplier)} burn until {clock} ({until}) — {burn.applies_to}"
    return (
        f"{format_multiplier(burn.multiplier)} burn — next {format_multiplier(burn.peak_multiplier)} window in {until}"
    )


def format_peak_schedule(burn: BurnStatus, tz: tzinfo | None = None) -> str | None:
    """Upcoming windows in local time, so the expensive hours can be planned around."""
    ahead = burn.upcoming[1:] if burn.in_peak else burn.upcoming
    if not ahead:
        return None
    windows = "  ".join(format_peak_interval(interval, tz) for interval in ahead)
    return f"{format_multiplier(burn.peak_multiplier)} windows (local): {windows}"
