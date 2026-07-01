from datetime import UTC, datetime

import pytest
import pytest_bazel
from syrupy.assertion import SnapshotAssertion

from aiquota.models import AllQuotas, FetchSuccess, ProviderFetch, ProviderQuota, QuotaWindow
from aiquota.render import human
from aiquota.testing.quota_fixtures import FIXTURE_NAMES, load_quota_fixture
from util.bazel.runfiles import get_required_path

if __name__ == "__main__":
    pytest_bazel.main()


_FETCHED_AT = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)


def _success(**kw) -> ProviderFetch:
    return ProviderFetch(fetched_at=_FETCHED_AT, result=FetchSuccess(**kw))


def _pq(provider: str, last_output: ProviderFetch) -> ProviderQuota:
    return ProviderQuota(provider=provider, last_output=last_output)


def _quotas(*providers: ProviderQuota) -> AllQuotas:
    return AllQuotas(providers=list(providers), fetched_at=_FETCHED_AT)


def test_renders_both_windows_with_reset_and_pace(snapshot: SnapshotAssertion) -> None:
    out = human.render(
        _quotas(
            _pq(
                "codex",
                _success(
                    short_window=QuotaWindow(used_percent=24, reset_seconds=3600 + 33 * 60, window_seconds=5 * 3600),
                    long_window=QuotaWindow(
                        used_percent=48, reset_seconds=5 * 86400 + 12 * 3600, window_seconds=7 * 86400
                    ),
                ),
            ),
            _pq(
                "zai",
                _success(
                    short_window=QuotaWindow(
                        used_percent=49, reset_seconds=2 * 3600 + 45 * 60, window_seconds=5 * 3600
                    ),
                    long_window=QuotaWindow(
                        used_percent=100, reset_seconds=6 * 86400 + 14 * 3600, window_seconds=7 * 86400
                    ),
                ),
            ),
        ),
        now=_FETCHED_AT,
    )
    assert out == snapshot


def test_aligns_reset_and_pace_columns_across_providers() -> None:
    out = human.render(
        _quotas(
            _pq(
                "claude",
                _success(
                    short_window=QuotaWindow(
                        used_percent=14, reset_seconds=2 * 3600 + 38 * 60, window_seconds=5 * 3600
                    ),
                    long_window=QuotaWindow(
                        used_percent=33, reset_seconds=2 * 86400 + 11 * 3600, window_seconds=7 * 86400
                    ),
                ),
            ),
            _pq(
                "codex",
                _success(
                    short_window=QuotaWindow(used_percent=19, reset_seconds=31 * 60, window_seconds=5 * 3600),
                    long_window=QuotaWindow(
                        used_percent=3, reset_seconds=6 * 86400 + 19 * 3600, window_seconds=7 * 86400
                    ),
                ),
            ),
            _pq(
                "zai",
                _success(
                    short_window=QuotaWindow(used_percent=0, reset_seconds=0, window_seconds=5 * 3600),
                    long_window=QuotaWindow(
                        used_percent=35, reset_seconds=15 * 3600 + 39 * 60, window_seconds=7 * 86400
                    ),
                ),
            ),
        ),
        now=_FETCHED_AT,
    )
    lines = out.splitlines()
    delta_columns = [line.index("Δ") for line in lines if "Δ" in line]
    forecast_columns = [line.index("leaves") for line in lines if "leaves" in line]
    assert len(set(delta_columns)) == 1
    assert len(set(forecast_columns)) == 1


@pytest.mark.parametrize("fixture_name", FIXTURE_NAMES)
def test_renders_shared_fixture(fixture_name: str, snapshot: SnapshotAssertion) -> None:
    fixture_path = get_required_path(f"_main/aiquota/testing/fixtures/{fixture_name}.yaml")
    quotas = load_quota_fixture(fixture_path, now=_FETCHED_AT)
    assert human.render(quotas, now=_FETCHED_AT) == snapshot
