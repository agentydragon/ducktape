from datetime import UTC, datetime
from pathlib import Path

import pytest_bazel

from aiquota.cache import QuotaCache, _assemble
from aiquota.models import AllQuotas, ProviderFetch, ProviderQuota, QuotaWindow

if __name__ == "__main__":
    pytest_bazel.main()


def test_cache_roundtrip(tmp_path: Path) -> None:
    cache = QuotaCache(path=tmp_path / "cache.json")
    now = datetime.now(UTC)
    quotas = AllQuotas(
        providers=[ProviderQuota(provider="test", last_output=ProviderFetch(error="none", fetched_at=now))],
        fetched_at=now,
    )
    cache.write(quotas)
    restored = cache.read()
    assert restored is not None
    assert restored.providers[0].provider == "test"


def test_cache_missing_returns_none(tmp_path: Path) -> None:
    cache = QuotaCache(path=tmp_path / "nonexistent.json")
    assert cache.read() is None


def test_cache_corrupt_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("not json")
    cache = QuotaCache(path=path)
    assert cache.read() is None


def test_assemble_records_success_for_clean_fetch() -> None:
    now = datetime.now(UTC)
    fetch = ProviderFetch(
        short_window=QuotaWindow(used_percent=10, reset_seconds=3600, window_seconds=18000), fetched_at=now
    )
    pq = _assemble("claude", fetch, prior=None)
    assert pq.last_output is fetch
    assert pq.last_success is fetch


def test_assemble_carries_prior_last_success_forward_on_error() -> None:
    now = datetime.now(UTC)
    prior_success = ProviderFetch(
        short_window=QuotaWindow(used_percent=35, reset_seconds=3600, window_seconds=18000), fetched_at=now
    )
    prior = ProviderQuota(
        provider="claude", last_output=ProviderFetch(error="HTTP 503", fetched_at=now), last_success=prior_success
    )
    errored = ProviderFetch(error="HTTP 504", fetched_at=now)
    pq = _assemble("claude", errored, prior=prior)
    assert pq.last_output is errored
    assert pq.last_success is prior_success


def test_assemble_clears_last_success_when_no_prior() -> None:
    now = datetime.now(UTC)
    errored = ProviderFetch(error="HTTP 503", fetched_at=now)
    pq = _assemble("claude", errored, prior=None)
    assert pq.last_output is errored
    assert pq.last_success is None
