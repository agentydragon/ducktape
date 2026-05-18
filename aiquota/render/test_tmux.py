from datetime import UTC, datetime, timedelta

import pytest_bazel

from aiquota.models import ProviderFetch, ProviderQuota, QuotaWindow
from aiquota.render.tmux import render, render_provider

if __name__ == "__main__":
    pytest_bazel.main()


_NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)


def _pq(provider: str, **kw) -> ProviderQuota:
    last_success = kw.pop("last_success", None)
    return ProviderQuota(provider=provider, last_output=ProviderFetch(fetched_at=_NOW, **kw), last_success=last_success)


def test_render_provider_with_data() -> None:
    pq = _pq("claude", long_window=QuotaWindow(used_percent=45.0, reset_seconds=86400.0, window_seconds=604800.0))
    result = render_provider(pq)
    assert "C:" in result
    assert "45%" in result
    assert "#[" in result


def test_render_provider_error_only() -> None:
    pq = _pq("codex", error="no auth")
    result = render_provider(pq)
    assert "W:!" in result
    assert "red" in result


def test_render_provider_no_windows() -> None:
    pq = _pq("zai")
    result = render_provider(pq)
    assert "Z:?" in result


def test_render_provider_falls_back_to_last_success_when_errored() -> None:
    pq = ProviderQuota(
        provider="claude",
        last_output=ProviderFetch(error="HTTP 503", fetched_at=_NOW),
        last_success=ProviderFetch(
            long_window=QuotaWindow(used_percent=72.0, reset_seconds=86400.0, window_seconds=604800.0),
            fetched_at=_NOW - timedelta(minutes=8),
        ),
    )
    result = render_provider(pq)
    assert "C:72%*" in result
    assert "yellow" in result  # stale tint


def test_render_multiple() -> None:
    providers = [
        _pq("claude", long_window=QuotaWindow(used_percent=50.0, reset_seconds=9000.0, window_seconds=18000.0)),
        _pq("codex", long_window=QuotaWindow(used_percent=90.0, reset_seconds=9000.0, window_seconds=18000.0)),
    ]
    result = render(providers)
    assert "C:" in result
    assert "W:" in result
    assert "50%" in result
    assert "90%" in result
