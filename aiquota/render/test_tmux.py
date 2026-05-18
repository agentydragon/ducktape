from datetime import UTC, datetime, timedelta

import pytest_bazel

from aiquota.models import FetchError, FetchSuccess, ProviderFetch, ProviderQuota, QuotaWindow, SuccessfulProviderFetch
from aiquota.render.tmux import _AI_GLYPH, render, render_provider

if __name__ == "__main__":
    pytest_bazel.main()


_NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)


def _success(**kw) -> ProviderFetch:
    return ProviderFetch(fetched_at=_NOW, result=FetchSuccess(**kw))


def _failure(error: str) -> ProviderFetch:
    return ProviderFetch(fetched_at=_NOW, result=FetchError(error=error))


def test_render_provider_with_data() -> None:
    pq = ProviderQuota(
        provider="claude",
        last_output=_success(
            long_window=QuotaWindow(used_percent=45.0, reset_seconds=86400.0, window_seconds=604800.0)
        ),
    )
    result = render_provider(pq)
    assert "A:" in result
    assert "45%" in result
    assert "#[" in result


def test_render_provider_error_only() -> None:
    pq = ProviderQuota(provider="codex", last_output=_failure("no auth"))
    result = render_provider(pq)
    assert "O:!" in result
    assert "red" in result


def test_render_provider_no_windows() -> None:
    pq = ProviderQuota(provider="zai", last_output=_success())
    result = render_provider(pq)
    assert "Z:?" in result


def test_render_provider_falls_back_to_last_success_when_errored() -> None:
    pq = ProviderQuota(
        provider="claude",
        last_output=_failure("HTTP 503"),
        last_success=SuccessfulProviderFetch(
            fetched_at=_NOW - timedelta(minutes=8),
            result=FetchSuccess(
                long_window=QuotaWindow(used_percent=72.0, reset_seconds=86400.0, window_seconds=604800.0)
            ),
        ),
    )
    result = render_provider(pq)
    assert "A:72%*" in result
    assert "yellow" in result  # stale tint


def test_render_multiple() -> None:
    providers = [
        ProviderQuota(
            provider="claude",
            last_output=_success(
                long_window=QuotaWindow(used_percent=50.0, reset_seconds=9000.0, window_seconds=18000.0)
            ),
        ),
        ProviderQuota(
            provider="codex",
            last_output=_success(
                long_window=QuotaWindow(used_percent=90.0, reset_seconds=9000.0, window_seconds=18000.0)
            ),
        ),
    ]
    result = render(providers)
    # Single sparkle prepended once to the whole segment, not per provider.
    assert result.startswith(f"{_AI_GLYPH} ")
    assert result.count(_AI_GLYPH) == 1
    assert "A:" in result
    assert "O:" in result
    assert "50%" in result
    assert "90%" in result


def test_render_empty_returns_empty_string() -> None:
    assert render([]) == ""
