from datetime import UTC, datetime

import pytest
import pytest_bazel
from pydantic import ValidationError

from aiquota.models import (
    AllQuotas,
    ExtraSpend,
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


def test_fetch_success_sorts_windows_by_provider_duration() -> None:
    weekly = QuotaWindow(used_percent=6, reset_seconds=1, window_seconds=604800)
    session = QuotaWindow(used_percent=2, reset_seconds=1, window_seconds=18000)

    assert FetchSuccess(windows=[weekly, session]).windows == [session, weekly]


def test_fetch_success_rejects_duplicate_durations() -> None:
    windows = [
        QuotaWindow(used_percent=6, reset_seconds=1, window_seconds=604800),
        QuotaWindow(used_percent=2, reset_seconds=2, window_seconds=604800),
    ]

    with pytest.raises(ValidationError, match="quota window identities must be unique"):
        FetchSuccess(windows=windows)


def test_provider_quota_error() -> None:
    pq = ProviderQuota(
        provider="test", last_output=ProviderFetch(fetched_at=_NOW, result=FetchError(error="something failed"))
    )
    assert isinstance(pq.last_output.result, FetchError)
    assert pq.last_output.result.error == "something failed"
    assert pq.last_success is None


def test_fetch_error_from_exception_uses_type_when_message_is_blank() -> None:
    err = FetchError.from_exception(TimeoutError(), "quota fetch")
    assert err.error == "quota fetch: TimeoutError"


def test_all_quotas_roundtrip() -> None:
    reset_at = datetime(2026, 1, 15, 13, 0, 0, tzinfo=UTC)
    payload = FetchSuccess(
        windows=[
            QuotaWindow(used_percent=72.0, reset_seconds=3600.0, window_seconds=18000.0, reset_at=reset_at),
            QuotaWindow(used_percent=45.0, reset_seconds=86400.0, window_seconds=604800.0),
        ],
        extra_spend=ExtraSpend(is_enabled=True, monthly_limit_usd=100.0, used_usd=62.83, utilization=62.83),
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
    assert p.last_output.result.windows[0].used_percent == 72.0
    assert p.last_output.result.windows[0].reset_at == reset_at
    assert p.last_output.result.extra_spend is not None
    assert p.last_output.result.extra_spend.monthly_limit_usd == 100.0
    assert p.last_success is not None
    assert p.last_success.result.windows[1].used_percent == 45.0
    assert restored.fetched_at == quotas.fetched_at
