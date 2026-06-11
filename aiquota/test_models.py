from datetime import UTC, datetime

import pytest_bazel

from aiquota.models import (
    AllQuotas,
    ExtraUsage,
    FetchError,
    FetchSuccess,
    ProviderFetch,
    ProviderQuota,
    QuotaWindow,
    SuccessfulProviderFetch,
)

if __name__ == "__main__":
    pytest_bazel.main()


_NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)


def test_quota_window_defaults() -> None:
    w = QuotaWindow(used_percent=50.0, reset_seconds=1800.0, window_seconds=18000.0)
    assert w.used_percent == 50.0
    assert w.reset_seconds == 1800.0
    assert w.window_seconds == 18000.0


def test_provider_quota_error() -> None:
    pq = ProviderQuota(
        provider="test", last_output=ProviderFetch(fetched_at=_NOW, result=FetchError(error="something failed"))
    )
    assert isinstance(pq.last_output.result, FetchError)
    assert pq.last_output.result.error == "something failed"
    assert pq.last_success is None


def test_all_quotas_roundtrip() -> None:
    reset_at = datetime(2026, 1, 15, 13, 0, 0, tzinfo=UTC)
    payload = FetchSuccess(
        short_window=QuotaWindow(used_percent=72.0, reset_seconds=3600.0, window_seconds=18000.0, reset_at=reset_at),
        long_window=QuotaWindow(used_percent=45.0, reset_seconds=86400.0, window_seconds=604800.0),
        extra_usage=ExtraUsage(is_enabled=True, monthly_limit_usd=100.0, used_usd=62.83, utilization=62.83),
    )
    quotas = AllQuotas(
        providers=[
            ProviderQuota(
                provider="claude",
                last_output=ProviderFetch(fetched_at=_NOW, result=payload),
                last_success=SuccessfulProviderFetch(fetched_at=_NOW, result=payload),
            )
        ],
        fetched_at=_NOW,
    )
    restored = AllQuotas.model_validate_json(quotas.model_dump_json())
    p = restored.providers[0]
    assert p.provider == "claude"
    assert isinstance(p.last_output.result, FetchSuccess)
    assert p.last_output.result.short_window is not None
    assert p.last_output.result.short_window.used_percent == 72.0
    assert p.last_output.result.short_window.reset_at == reset_at
    assert p.last_output.result.extra_usage is not None
    assert p.last_output.result.extra_usage.monthly_limit_usd == 100.0
    assert p.last_success is not None
    assert p.last_success.result.long_window is not None
    assert restored.fetched_at == quotas.fetched_at
