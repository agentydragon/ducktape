from aiquota.models import AllQuotas, FetchSuccess, ProviderQuota
from aiquota.render.view_model import ProviderView, to_view

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


def render_provider(pq: ProviderQuota) -> str:
    return _render_provider(to_view(AllQuotas(providers=[pq], fetched_at=pq.last_output.fetched_at)).providers[0])


def _render_provider(pq: ProviderView) -> str:
    prefix = PROVIDER_PREFIX.get(pq.provider, pq.provider[0].upper())
    result = pq.last_output.result

    # Pick the windows to render: prefer the latest fetch when it succeeded,
    # else fall back to the last successful snapshot.
    if isinstance(result, FetchSuccess) and (
        result.windows
        or result.available_reset_credits is not None
        or result.paid_credits is not None
        or result.prepaid_balance is not None
    ):
        windows, stale = pq.display_windows, False
    elif pq.last_success is not None:
        windows, stale = pq.display_windows, True
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

    color = TINT_FG[pq.summary_tint]

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
