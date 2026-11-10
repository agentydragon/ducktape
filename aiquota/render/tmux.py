"""Compact tmux status summary, using the shared provider window selection."""

from aiquota.models import AllQuotas, ProviderQuota
from aiquota.render.view_model import ProviderView, to_view

# Nerd Font cod-sparkle (U+EC10), the de-facto "AI" glyph in dev tooling.
_AI_GLYPH = "\uec10"
PROVIDER_PREFIX = {"claude": "A", "codex": "O", "zai": "Z"}
_API_PROVIDERS = {"openai_platform", "anthropic_platform"}

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
    if pq.provider in _API_PROVIDERS:
        return ""
    prefix = PROVIDER_PREFIX.get(pq.provider, pq.provider[0].upper())
    windows = pq.display_windows
    index = pq.summary_index
    summary = windows[index] if index is not None and 0 <= index < len(windows) else None
    stale = pq.stale_since is not None

    if any(window.reset_pending for window in windows):
        value = "↻?"
        color = "stale"
    elif pq.paid_credits_active or pq.currently_over_plan:
        value = "⚡"
        color = pq.summary_tint
    elif summary is not None and summary.exhausted:
        last_reset = max((window for window in windows if window.exhausted), key=lambda window: window.reset_seconds)
        value = f"↻{last_reset.reset.replace(' ', '')}"
        color = summary.tint
    elif summary is not None:
        value = f"{summary.used}%"
        color = summary.tint
    elif pq.error_summary is not None:
        value = "!"
        color = "error"
    else:
        value = "?"
        color = pq.summary_tint

    if stale:
        value += "*"
        color = "stale"
    return f"#[fg={TINT_FG[color]}]{prefix}:{value}#[default]"


def render_view(providers: list[ProviderView]) -> str:
    segments = [_render_provider(provider) for provider in providers if provider.provider not in _API_PROVIDERS]
    return f"{_AI_GLYPH} " + " ".join(segments) if segments else ""


def render(providers: list[ProviderQuota]) -> str:
    segments = [render_provider(provider) for provider in providers if provider.provider not in _API_PROVIDERS]
    return f"{_AI_GLYPH} " + " ".join(segments) if segments else ""
