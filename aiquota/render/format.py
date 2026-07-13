"""Shared format helpers for human and tmux renderers."""

from aiquota.models import PaceResult, QuotaWindow


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
