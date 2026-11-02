from datetime import UTC, datetime, timedelta, timezone

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
from aiquota.render.format import format_prepaid_balance, format_reset_credit_summary
from aiquota.render.view_model import to_view
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

    assert "Claude · $123.45 prepaid balance" in rendered
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
    assert "Claude · $123.45 prepaid balance" in rendered
    assert "(stale 1h00m)" in rendered


def test_cli_preserves_independent_prepaid_and_quota_staleness() -> None:
    older = _FETCHED_AT - timedelta(hours=1)
    balance_older = _FETCHED_AT - timedelta(hours=2)
    last_success = SuccessfulProviderFetch(
        fetched_at=older,
        result=FetchSuccess(
            windows=[QuotaWindow(used_percent=25, reset_seconds=60, window_seconds=3600)],
            prepaid_balance=PrepaidBalanceAvailable(amount_minor=12345, exponent=2, currency="USD"),
        ),
    )
    quota = ProviderQuota(
        provider="claude",
        last_output=ProviderFetch(fetched_at=_FETCHED_AT, result=FetchError(error="temporary usage refresh failure")),
        last_success=last_success,
    )
    provider = to_view(_quotas(quota), now=_FETCHED_AT).providers[0]
    provider = provider.model_copy(
        update={"display": provider.display.model_copy(update={"prepaid_stale_since": balance_older})}
    )

    rendered = human._render_provider(provider, _FETCHED_AT, human._ColumnWidths(), UTC).plain

    assert "(stale 1h00m)" in rendered
    assert "(prepaid stale 2h00m)" in rendered


def test_openai_platform_grants_survive_a_failed_refresh_as_stale() -> None:
    fixture = load_quota_fixture(get_required_path("_main/aiquota/testing/fixtures/openai_platform_credits.yaml"))
    previous = fixture.providers[0].last_output
    assert isinstance(previous.result, FetchSuccess)
    quota = ProviderQuota(
        provider="openai_platform",
        last_output=ProviderFetch(fetched_at=_FETCHED_AT, result=FetchError(error="proxy unavailable")),
        last_success=SuccessfulProviderFetch(fetched_at=_FETCHED_AT - timedelta(hours=1), result=previous.result),
    )
    rendered = human.render(_quotas(quota), now=_FETCHED_AT, tz=UTC)
    assert "proxy unavailable" in rendered
    assert "OpenAI API · $125.75 API credit" in rendered
    assert (
        "API credit grants: $100.00 expires 2026-02-01 00:00 UTC; $25.75 expires 2026-12-01 00:00 UTC (stale)"
        in rendered
    )
    assert "expires 2026-02-01 00:00 UTC" in rendered


def test_anthropic_platform_lists_org_balances_and_active_scoped_grants() -> None:
    fixture = load_quota_fixture(get_required_path("_main/aiquota/testing/fixtures/anthropic_platform_credits.yaml"))

    rendered = human.render(fixture, now=_FETCHED_AT, tz=UTC)

    assert rendered.startswith("Anthropic API\n")
    assert "Main org · Anthropic API credit balance · $123.45 available" in rendered
    assert "Secondary org · Anthropic API credit balance · $50.00 available" in rendered
    assert "API credit grant $85.00 / $100.00 · expires 2026-07-01 00:00 UTC" in rendered
    assert "scope: unscoped" not in rendered
    assert (
        "API promo credit grant $25.00 / $30.00 · expires 2026-05-01 00:00 UTC · scope: claude_api (sonnet), claude_code"
        in rendered
    )
    assert "Expired grant" not in rendered


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
        "codex · 2 resets (expiry: Jan 20, 09:00, 1 unknown)\n"
    )


@pytest.mark.parametrize(
    ("count", "expiries", "lookup_failed", "expected"),
    [
        (1, [datetime(2026, 1, 20, 9, 0, tzinfo=UTC)], False, "1 reset (expiry: Jan 20, 09:00)"),
        (
            2,
            [datetime(2026, 1, 20, 9, 0, tzinfo=UTC), datetime(2026, 2, 2, 12, 34, tzinfo=UTC)],
            False,
            "2 resets (expiry: Jan 20, 09:00, Feb 2)",
        ),
        (2, [], False, "2 resets (expiry: 2 unknown)"),
        (2, [], True, "2 resets (expiry lookup failed)"),
        (2, [datetime(2026, 1, 20, 9, 0, tzinfo=UTC)], True, "2 resets (expiry: Jan 20, 09:00; lookup failed)"),
    ],
)
def test_formats_reset_credit_count_and_expiry_outcomes(count, expiries, lookup_failed, expected) -> None:
    assert format_reset_credit_summary(count, expiries, lookup_failed, _FETCHED_AT, UTC) == expected


@pytest.mark.parametrize(
    ("expiry", "expected"),
    [
        (datetime(2026, 1, 29, 11, 59, 59, tzinfo=UTC), "Jan 29, 11:59"),
        (datetime(2026, 1, 29, 12, 0, tzinfo=UTC), "Jan 29, 12:00"),
        (datetime(2026, 1, 29, 12, 0, 1, tzinfo=UTC), "Jan 29"),
        (datetime(2026, 1, 14, 9, 0, tzinfo=UTC), "Jan 14, 09:00"),
    ],
)
def test_reset_expiry_time_is_omitted_only_when_more_than_two_weeks_away(expiry, expected) -> None:
    assert format_reset_credit_summary(1, [expiry], False, _FETCHED_AT, UTC) == f"1 reset (expiry: {expected})"


def test_reset_expiry_cutoff_uses_viewer_timezone_for_rendering() -> None:
    pacific = timezone(timedelta(hours=-8))
    expiries = [datetime(2026, 1, 30, 5, 0, tzinfo=UTC), datetime(2026, 1, 29, 11, 59, 59, tzinfo=UTC)]

    assert format_reset_credit_summary(2, expiries, False, _FETCHED_AT, pacific) == (
        "2 resets (expiry: Jan 29, Jan 29, 03:59)"
    )


def test_combines_subscription_resets_and_separate_platform_credit_pools() -> None:
    fixture = load_quota_fixture(
        get_required_path("_main/aiquota/testing/fixtures/platform_credits_with_subscription_resets.yaml")
    )

    rendered = human.render(fixture, now=_FETCHED_AT, tz=UTC)

    assert "Claude · $25.75 prepaid balance" in rendered
    assert "5h:  45%" in rendered
    assert "7d:  32%" in rendered
    assert "codex · 3 resets (expiry: Jan 29, 12:00, Feb 1, 1 unknown)" in rendered
    assert "OpenAI API · $60.00 API credit" in rendered
    assert "API credit grants: $60.00 expires 2026-06-01 00:00 UTC" in rendered
    assert "Shared org · Anthropic API credit balance · $75.00 available" in rendered
    assert "API credit grant $50.00 / $100.00 · expires 2026-07-01 00:00 UTC" in rendered
    assert (
        "API promo credit grant $25.00 / $50.00 · expires 2026-05-01 00:00 UTC · scope: claude_api (sonnet)" in rendered
    )


@pytest.mark.parametrize("fixture_name", FIXTURE_NAMES)
def test_renders_shared_fixture(fixture_name: str, snapshot: SnapshotAssertion) -> None:
    fixture_path = get_required_path(f"_main/aiquota/testing/fixtures/{fixture_name}.yaml")
    quotas = load_quota_fixture(fixture_path)
    assert human.render(quotas, now=_FETCHED_AT, tz=UTC) == snapshot


def test_terminal_window_uses_shared_tint() -> None:
    quotas = _quotas(
        _pq("claude", _success(short_window=QuotaWindow(used_percent=60, reset_seconds=9000, window_seconds=18000)))
    )
    rendered = human.render_text(quotas, now=_FETCHED_AT, tz=UTC)
    assert "Δ+10%" in rendered.plain
    assert any(span.style == "yellow" for span in rendered.spans)
