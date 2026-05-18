from aiquota.models import ProviderQuota, QuotaWindow
from aiquota.pace import binding_tint, compute_pace, tint_for

PROVIDER_PREFIX = {"claude": "C", "codex": "W", "zai": "Z"}

TINT_FG = {
    "cool": "blue",
    "ok": "white",
    "warn": "yellow",
    "hot": "red",
    "unknown": "white",
    "stale": "yellow",
    "error": "red",
}


def _window_tint(window: QuotaWindow | None, *, is_short: bool) -> str:
    if window is None:
        return "unknown"
    pace = compute_pace(window)
    return tint_for(pace, window.used_percent, is_short=is_short)


def render_provider(pq: ProviderQuota) -> str:
    prefix = PROVIDER_PREFIX.get(pq.provider, pq.provider[0].upper())
    out = pq.last_output

    # If the latest call gave us nothing, fall back to the last successful snapshot.
    short = out.short_window
    long = out.long_window
    stale = False
    if short is None and long is None and pq.last_success is not None:
        snap = pq.last_success
        if snap.short_window is not None or snap.long_window is not None:
            short = snap.short_window
            long = snap.long_window
            stale = True

    if out.error and short is None and long is None:
        return f"#[fg={TINT_FG['error']}]{prefix}:!#[default]"

    if stale:
        w = long or short
        if w is None:
            return f"#[fg={TINT_FG['stale']}]{prefix}:?#[default]"
        return f"#[fg={TINT_FG['stale']}]{prefix}:{round(w.used_percent)}%*#[default]"

    short_tint = _window_tint(short, is_short=True)
    long_tint = _window_tint(long, is_short=False)
    tint = binding_tint(short_tint, long_tint)
    color = TINT_FG.get(tint, "white")

    # Show the more informative window (prefer long if available)
    w = long or short
    if w is None:
        return f"#[fg={color}]{prefix}:?#[default]"

    pct = round(w.used_percent)
    return f"#[fg={color}]{prefix}:{pct}%#[default]"


def render(providers: list[ProviderQuota]) -> str:
    return " ".join(render_provider(pq) for pq in providers)
