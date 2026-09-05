from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest_bazel

from aiquota.cache import QuotaCache, _assemble
from aiquota.models import (
    AllQuotas,
    FetchError,
    FetchSuccess,
    ProviderFetch,
    ProviderQuota,
    QuotaWindow,
    SuccessfulProviderFetch,
)
from aiquota.providers.base import Provider

if __name__ == "__main__":
    pytest_bazel.main()


def test_cache_roundtrip(tmp_path: Path) -> None:
    cache = QuotaCache(path=tmp_path / "cache.json")
    now = datetime.now(UTC)
    quotas = AllQuotas(
        providers=[
            ProviderQuota(provider="test", last_output=ProviderFetch(fetched_at=now, result=FetchError(error="none")))
        ],
        fetched_at=now,
    )
    cache.write(quotas)
    restored = cache.read()
    assert restored is not None
    assert restored.providers[0].provider == "test"
    assert isinstance(restored.providers[0].last_output.result, FetchError)


def test_cache_missing_returns_none(tmp_path: Path) -> None:
    cache = QuotaCache(path=tmp_path / "nonexistent.json")
    assert cache.read() is None


def test_cache_corrupt_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text("not json")
    cache = QuotaCache(path=path)
    assert cache.read() is None


class _CountingProvider(Provider):
    name = "test"

    def __init__(self) -> None:
        self.fetch_count = 0

    async def fetch(self) -> ProviderFetch:
        self.fetch_count += 1
        return _success(datetime.now(UTC))


async def test_force_refresh_bypasses_fresh_cache(tmp_path: Path) -> None:
    cache = QuotaCache(path=tmp_path / "cache.json")
    provider = _CountingProvider()

    await cache.fetch_all([provider])
    await cache.fetch_all([provider])
    await cache.fetch_all([provider], force_refresh=True)

    assert provider.fetch_count == 2


async def test_remote_refresh_falls_back_to_stale_local_snapshot(tmp_path: Path) -> None:
    cache = QuotaCache(path=tmp_path / "cache.json")
    cached = AllQuotas(providers=[], fetched_at=datetime.now(UTC) - timedelta(hours=1))
    cache.write(cached)

    async def unavailable() -> AllQuotas:
        raise OSError("offline")

    assert await cache.fetch_remote(unavailable, force_refresh=True) == cached


def _success(
    now: datetime, short_window: QuotaWindow | None = None, long_window: QuotaWindow | None = None
) -> ProviderFetch:
    return ProviderFetch(
        fetched_at=now, result=FetchSuccess(windows=[window for window in (short_window, long_window) if window])
    )


def _error(now: datetime, msg: str) -> ProviderFetch:
    return ProviderFetch(fetched_at=now, result=FetchError(error=msg))


def test_assemble_records_success_for_clean_fetch() -> None:
    now = datetime.now(UTC)
    fetch = _success(now, short_window=QuotaWindow(used_percent=10, reset_seconds=3600, window_seconds=18000))
    pq = _assemble("claude", fetch, prior=None)
    assert pq.last_output is fetch
    assert pq.last_success is not None
    assert pq.last_success.fetched_at == now
    assert pq.last_success.result is fetch.result


def test_assemble_carries_prior_last_success_forward_on_error() -> None:
    now = datetime.now(UTC)
    prior_success = SuccessfulProviderFetch(
        fetched_at=now,
        result=FetchSuccess(windows=[QuotaWindow(used_percent=35, reset_seconds=3600, window_seconds=18000)]),
    )
    prior = ProviderQuota(provider="claude", last_output=_error(now, "HTTP 503"), last_success=prior_success)
    errored = _error(now, "HTTP 504")
    pq = _assemble("claude", errored, prior=prior)
    assert pq.last_output is errored
    assert pq.last_success is prior_success


def test_assemble_clears_last_success_when_no_prior() -> None:
    now = datetime.now(UTC)
    errored = _error(now, "HTTP 503")
    pq = _assemble("claude", errored, prior=None)
    assert pq.last_output is errored
    assert pq.last_success is None


def test_assemble_keeps_prior_last_success_when_fresh_success_has_no_data() -> None:
    # codex returns FetchSuccess with both windows None on fresh accounts.
    # That "successful but empty" response shouldn't overwrite a prior good
    # snapshot.
    now = datetime.now(UTC)
    prior_success = SuccessfulProviderFetch(
        fetched_at=now,
        result=FetchSuccess(windows=[QuotaWindow(used_percent=72, reset_seconds=86400, window_seconds=604800)]),
    )
    prior = ProviderQuota(provider="codex", last_output=_success(now), last_success=prior_success)
    empty = _success(now)
    pq = _assemble("codex", empty, prior=prior)
    assert pq.last_success is prior_success


def test_assemble_records_banked_resets_without_windows() -> None:
    now = datetime.now(UTC)
    fetch = ProviderFetch(fetched_at=now, result=FetchSuccess(available_reset_credits=2))

    pq = _assemble("codex", fetch, prior=None)

    assert pq.last_success is not None
    assert pq.last_success.result.available_reset_credits == 2
