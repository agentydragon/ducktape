import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest_bazel

from aiquota.cache import QuotaCache, _assemble
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
from aiquota.providers.base import Provider
from aiquota.render.view_model import AllQuotasView, DisplaySelection, to_view

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


def test_cache_roundtrip_preserves_reset_credit_expiry_lookup_failure(tmp_path: Path) -> None:
    cache = QuotaCache(path=tmp_path / "cache.json")
    now = datetime.now(UTC)
    output = ProviderFetch(
        fetched_at=now, result=FetchSuccess(available_reset_credits=2, reset_credit_expiry_lookup_failed=True)
    )
    provider = _assemble("codex", output, prior=None)
    cache.write(AllQuotas(providers=[provider], fetched_at=now))

    restored = cache.read()

    assert restored is not None
    restored_provider = restored.providers[0]
    assert isinstance(restored_provider.last_output.result, FetchSuccess)
    assert (
        restored_provider.last_output.result.available_reset_credits,
        restored_provider.last_output.result.reset_credit_expiry_lookup_failed,
    ) == (2, True)
    assert restored_provider.last_success is not None
    assert (
        restored_provider.last_success.result.available_reset_credits,
        restored_provider.last_success.result.reset_credit_expiry_lookup_failed,
    ) == (2, True)


def test_cache_reads_reset_credit_snapshot_without_new_failure_flag(tmp_path: Path) -> None:
    now = datetime.now(UTC).isoformat()
    path = tmp_path / "legacy-cache.json"
    path.write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "provider": "codex",
                        "last_output": {"fetched_at": now, "result": {"kind": "success", "available_reset_credits": 2}},
                        "last_success": {
                            "fetched_at": now,
                            "result": {"kind": "success", "available_reset_credits": 2},
                        },
                    }
                ],
                "fetched_at": now,
            }
        )
    )

    restored = QuotaCache(path=path).read()

    assert restored is not None
    provider = restored.providers[0]
    assert isinstance(provider.last_output.result, FetchSuccess)
    assert provider.last_output.result.available_reset_credits == 2
    assert provider.last_output.result.reset_credit_expiry_lookup_failed is False
    assert provider.last_success is not None
    assert provider.last_success.result.available_reset_credits == 2
    assert provider.last_success.result.reset_credit_expiry_lookup_failed is False


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

    async def unavailable() -> AllQuotasView:
        raise OSError("offline")

    view = await cache.fetch_remote_view(unavailable, force_refresh=True)
    assert view.connection_error == "offline"
    assert view.fetched_at == cached.fetched_at


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


def test_spend_only_observation_replaces_old_windows_without_relabeling_them_fresh() -> None:
    observed = datetime(2026, 1, 15, 12, tzinfo=UTC)
    prior = _assemble(
        "claude",
        _success(
            observed - timedelta(minutes=5),
            short_window=QuotaWindow(used_percent=60, reset_seconds=3600, window_seconds=18000),
        ),
        prior=None,
    )
    spend = ExtraSpend(is_enabled=False, monthly_limit_usd=100, used_usd=25.5, utilization=25.5)
    current = _assemble(
        "claude", ProviderFetch(fetched_at=observed, result=FetchSuccess(extra_spend=spend)), prior=prior
    )
    fresh = to_view(AllQuotas(providers=[current], fetched_at=observed), observed).providers[0]
    assert fresh.monthly_extra_spend == spend
    assert fresh.display.observed_at == observed
    assert fresh.display_windows == []
    failed_at = observed + timedelta(minutes=1)
    failed = _assemble("claude", _error(failed_at, "offline"), prior=current)
    fallback = to_view(AllQuotas(providers=[failed], fetched_at=failed_at), failed_at).providers[0]
    assert fallback.monthly_extra_spend == spend
    assert fallback.display.observed_at == observed
    assert fallback.stale_since == observed
    assert fallback.display_windows == []


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


async def test_remote_view_cache_preserves_server_selection(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    fetch = _success(now, short_window=QuotaWindow(used_percent=10, reset_seconds=3600, window_seconds=18000))
    view = to_view(AllQuotas(providers=[ProviderQuota(provider="test", last_output=fetch)], fetched_at=now), now)
    # A server selection must survive caching even if local selection policy changes.
    view.providers[0].display = DisplaySelection(source="none", observed_at=None, result=None)
    cache = QuotaCache(path=tmp_path / "cache.json")

    async def fetch_view() -> AllQuotasView:
        return view

    fetched = await cache.fetch_remote_view(fetch_view, force_refresh=True)
    assert fetched.providers[0].display == view.providers[0].display

    async def unavailable() -> AllQuotasView:
        raise OSError("offline")

    offline = await cache.fetch_remote_view(unavailable, force_refresh=True)
    assert offline.connection_error == "offline"
    assert offline.providers[0].display == view.providers[0].display
    assert offline.providers[0].display_windows == []
    assert cache.read() is not None  # the desktop's raw-cache reader remains compatible


async def test_cached_view_recomputes_deadlines_without_rewriting_observations(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    observed = now - timedelta(seconds=90)
    cache = QuotaCache(path=tmp_path / "cache.json")
    window = QuotaWindow(
        used_percent=100, reset_seconds=60, window_seconds=18000, reset_at=observed + timedelta(seconds=60)
    )
    fetch = _success(observed, short_window=window)
    view = to_view(
        AllQuotas(providers=[ProviderQuota(provider="codex", last_output=fetch)], fetched_at=observed), observed
    )
    original = view.model_dump_json()
    cache.path.write_text(original)

    async def unexpected_fetch() -> AllQuotasView:
        raise AssertionError("fresh cache must not contact server")

    refreshed = await cache.fetch_remote_view(unexpected_fetch)
    assert refreshed.providers[0].display_windows[0].reset_pending
    assert refreshed.providers[0].display.result == view.providers[0].display.result
    assert cache.path.read_text() == original
