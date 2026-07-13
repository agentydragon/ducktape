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


def _window_tint(window: QuotaWindow, *, is_short: bool) -> str:
    pace = compute_pace(window)
    return tint_for(pace, window.used_percent, is_short=is_short)


def render_provider(pq: ProviderQuota) -> str:
    prefix = PROVIDER_PREFIX.get(pq.provider, pq.provider[0].upper())
    result = pq.last_output.result

    # Pick the windows to render: prefer the latest fetch when it succeeded,
    # else fall back to the last successful snapshot.
    if isinstance(result, FetchSuccess) and result.windows:
        windows, stale = [window for window in result.windows if window.display], False
    elif pq.last_success is not None:
        windows, stale = [window for window in pq.last_success.result.windows if window.display], True
    else:
        windows = []
        stale = False

    errored = not isinstance(result, FetchSuccess)
    if errored and not windows:
        return f"#[fg={TINT_FG['error']}]{prefix}:!#[default]"

    if stale:
        if not windows:
            return f"#[fg={TINT_FG['stale']}]{prefix}:?#[default]"
        summary = next((window for window in reversed(windows) if window.name is None), windows[-1])
        return f"#[fg={TINT_FG['stale']}]{prefix}:{round(summary.used_percent)}%*#[default]"

    longest_duration = max((window.window_seconds for window in windows), default=0)
    tints = [_window_tint(window, is_short=window.window_seconds < longest_duration) for window in windows]
    tint = tints[0] if tints else "unknown"
    for next_tint in tints[1:]:
        tint = binding_tint(tint, next_tint)
    color = TINT_FG.get(tint, "white")

    # Show the more informative window (prefer long if available)
    if not windows:
        return f"#[fg={color}]{prefix}:?#[default]"

    summary = next((window for window in reversed(windows) if window.name is None), windows[-1])
    pct = round(summary.used_percent)
    return f"#[fg={color}]{prefix}:{pct}%#[default]"


def render(providers: list[ProviderQuota]) -> str:
    segments = [render_provider(pq) for pq in providers]
    if not segments:
        return ""
    return f"{_AI_GLYPH} " + " ".join(segments)
