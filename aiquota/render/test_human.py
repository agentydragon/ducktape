from datetime import UTC, datetime, timedelta

import pytest
import pytest_bazel
from syrupy.assertion import SnapshotAssertion

from aiquota.models import (
    AllQuotas,
    FetchError,
    FetchSuccess,
    PrepaidBalanceAvailable,
    ProviderFetch,
    ProviderQuota,
    QuotaWindow,
    SuccessfulProviderFetch,
)
from aiquota.render import human
from aiquota.render.format import format_prepaid_balance
from aiquota.testing.quota_fixtures import FIXTURE_NAMES, load_quota_fixture
from util.bazel.runfiles import get_required_path

if __name__ == "__main__":
    pytest_bazel.main()


_FETCHED_AT = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)


def _success(short_window: QuotaWindow | None = None, long_window: QuotaWindow | None = None) -> ProviderFetch:
    return ProviderFetch(
        fetched_at=_FETCHED_AT,
        result=FetchSuccess(windows=[window for window in (short_window, long_window) if window]),
    )


def _pq(provider: str, last_output: ProviderFetch) -> ProviderQuota:
    return ProviderQuota(provider=provider, last_output=last_output)


def _quotas(*providers: ProviderQuota) -> AllQuotas:
    return AllQuotas(providers=list(providers), fetched_at=_FETCHED_AT)


@pytest.mark.parametrize("used_percent", [100, 105])
def test_exhausted_window_suppresses_pace_and_projection(used_percent: float) -> None:
    # A provider with no burn schedule, so the whole-output equality below stays
    # about pace suppression rather than also pinning the peak-window lines.
    out = human.render(
        _quotas(
            _pq(
                "codex",
                _success(
                    short_window=QuotaWindow(
                        used_percent=used_percent, reset_seconds=3 * 3600 + 60, window_seconds=5 * 3600
                    )
                ),
            )
        ),
        now=_FETCHED_AT,
        tz=UTC,
    )

    assert out == f"codex\n  5h: {round(used_percent):>3d}%  ↻ 3h01m  exhausted"


def test_subthreshold_usage_does_not_render_as_exhausted() -> None:
    out = human.render(
        _quotas(
            _pq(
                "zai",
                _success(
                    short_window=QuotaWindow(used_percent=99.9, reset_seconds=3 * 3600 + 60, window_seconds=5 * 3600)
                ),
            )
        ),
        now=_FETCHED_AT,
        tz=UTC,
    )

    assert "5h:  99%" in out
    assert "exhausted" not in out


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
        tz=UTC,
    )
    lines = out.splitlines()
    delta_columns = [line.index("Δ") for line in lines if "Δ" in line]
    forecast_columns = [line.index("leaves") for line in lines if "leaves" in line]
    assert len(set(delta_columns)) == 1
    assert len(set(forecast_columns)) == 1


def test_renders_provider_supplied_duration_and_name() -> None:
    output = ProviderFetch(
        fetched_at=_FETCHED_AT,
        result=FetchSuccess(
            windows=[
                QuotaWindow(name="model-specific", used_percent=12, reset_seconds=12 * 3600, window_seconds=24 * 3600)
            ]
        ),
    )

    assert "model-specific (1d):  12%" in human.render(_quotas(_pq("codex", output)), now=_FETCHED_AT, tz=UTC)


def test_hidden_window_remains_in_model_but_is_not_rendered() -> None:
    output = ProviderFetch(
        fetched_at=_FETCHED_AT,
        result=FetchSuccess(
            windows=[
                QuotaWindow(used_percent=7, reset_seconds=3600, window_seconds=7 * 86400),
                QuotaWindow(name="Spark", display=False, used_percent=0, reset_seconds=3600, window_seconds=7 * 86400),
            ]
        ),
    )
    rendered = human.render(_quotas(_pq("codex", output)), now=_FETCHED_AT, tz=UTC)
    assert "7d:   7%" in rendered
    assert "Spark" not in rendered
    assert isinstance(output.result, FetchSuccess)
    assert len(output.result.windows) == 2


@pytest.mark.parametrize(
    ("amount_minor", "exponent", "currency", "expected"),
    [
        (12345, 2, "USD", "$123.45"),
        (123, 0, "USD", "$123"),
        (1234567, 3, "EUR", "1,234.567 EUR"),
        (0, 2, "USD", "$0.00"),
        (-123, 2, "USD", "-$1.23"),
    ],
)
def test_formats_exact_prepaid_minor_units(amount_minor: int, exponent: int, currency: str, expected: str) -> None:
    assert (
        format_prepaid_balance(PrepaidBalanceAvailable(amount_minor=amount_minor, exponent=exponent, currency=currency))
        == expected
    )


def test_cli_renders_claude_prepaid_balance_separately_from_codex_credits() -> None:
    output = ProviderFetch(
        fetched_at=_FETCHED_AT,
        result=FetchSuccess(prepaid_balance=PrepaidBalanceAvailable(amount_minor=12345, exponent=2, currency="USD")),
    )

    rendered = human.render(_quotas(_pq("claude", output)), now=_FETCHED_AT, tz=UTC)

    assert "Claude prepaid balance · $123.45" in rendered
    assert "paid credits" not in rendered
    assert "no data" not in rendered


def test_cli_marks_last_successful_prepaid_balance_stale_after_refresh_error() -> None:
    last_success = SuccessfulProviderFetch(
        fetched_at=_FETCHED_AT - timedelta(hours=1),
        result=FetchSuccess(prepaid_balance=PrepaidBalanceAvailable(amount_minor=12345, exponent=2, currency="USD")),
    )
    quota = ProviderQuota(
        provider="claude",
        last_output=ProviderFetch(fetched_at=_FETCHED_AT, result=FetchError(error="temporary usage refresh failure")),
        last_success=last_success,
    )

    rendered = human.render(_quotas(quota), now=_FETCHED_AT, tz=UTC)

    assert "last refresh failed" in rendered
    assert "Claude prepaid balance · $123.45 (stale 1h00m)" in rendered


def test_renders_banked_reset_count_from_live_usage() -> None:
    output = ProviderFetch(
        fetched_at=_FETCHED_AT,
        result=FetchSuccess(
            windows=[QuotaWindow(used_percent=12, reset_seconds=12 * 3600, window_seconds=24 * 3600)],
            available_reset_credits=2,
            available_reset_credit_expiries=[datetime(2026, 1, 20, 9, 0, tzinfo=UTC)],
        ),
    )

    assert human.render(_quotas(_pq("codex", output)), now=_FETCHED_AT, tz=UTC).startswith(
        "codex · 2 banked resets · known expiries: Jan 20 09:00\n"
    )


@pytest.mark.parametrize("fixture_name", FIXTURE_NAMES)
def test_renders_shared_fixture(fixture_name: str, snapshot: SnapshotAssertion) -> None:
    fixture_path = get_required_path(f"_main/aiquota/testing/fixtures/{fixture_name}.yaml")
    quotas = load_quota_fixture(fixture_path)
    assert human.render(quotas, now=_FETCHED_AT, tz=UTC) == snapshot
