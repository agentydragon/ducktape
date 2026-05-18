from aiquota.models import FetchSuccess, ProviderQuota, QuotaWindow
from aiquota.pace import binding_tint, compute_pace, tint_for

# Nerd Font cod-sparkle (U+EC10), the de-facto "AI" glyph in dev tooling.
# Prepended once to the whole segment; each provider then uses its vendor
# initial: Anthropic / OpenAI / Z.AI.
_AI_GLYPH = "\uec10"
PROVIDER_PREFIX = {"claude": "A", "codex": "O", "zai": "Z"}

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
    result = pq.last_output.result

    # Pick the windows to render: prefer the latest fetch when it succeeded,
    # else fall back to the last successful snapshot.
    if isinstance(result, FetchSuccess) and (result.short_window or result.long_window):
        short, long, stale = result.short_window, result.long_window, False
    elif pq.last_success is not None:
        short, long, stale = pq.last_success.result.short_window, pq.last_success.result.long_window, True
    else:
        short = long = None
        stale = False

    errored = not isinstance(result, FetchSuccess)
    if errored and short is None and long is None:
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
    segments = [render_provider(pq) for pq in providers]
    if not segments:
        return ""
    return f"{_AI_GLYPH} " + " ".join(segments)
