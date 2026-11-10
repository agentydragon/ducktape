from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone

import pytest
import pytest_bazel
from python.runfiles import Runfiles
from syrupy.assertion import SnapshotAssertion

from aiquota.models import AllQuotas, FetchSuccess, ProviderFetch, ProviderQuota, QuotaWindow
from aiquota.render import human
from aiquota.render.format import format_deadline, format_reset_credit_summary
from aiquota.render.view_model import to_view
from aiquota.testing.quota_fixtures import FIXTURE_NAMES, load_quota_fixture

if __name__ == "__main__":
    pytest_bazel.main()


_NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def quota_fixture(runfiles: Runfiles) -> Callable[[str], AllQuotas]:
    root = runfiles.root()

    def load(name: str) -> AllQuotas:
        return load_quota_fixture(root / f"ducktape/aiquota/testing/fixtures/{name}.yaml")

    return load


def _quota(result: FetchSuccess) -> AllQuotas:
    fetched = ProviderFetch(fetched_at=_NOW, result=result)
    return AllQuotas(providers=[ProviderQuota(provider="codex", last_output=fetched)], fetched_at=_NOW)


def test_default_rows_keep_balances_grants_groups_and_subscription_windows_separate(
    quota_fixture: Callable[[str], AllQuotas],
) -> None:
    rendered = human.render(quota_fixture("platform_credits_with_subscription_resets"), now=_NOW, tz=UTC)

    assert rendered.startswith("Subscriptions\n\nClaude\n  Prepaid balance: $25.75")
    assert "5h:  45% used  ↻ 2h" in rendered
    assert "Codex\n  Banked resets: 3 · next known expiry Jan 29, 12:00 UTC" in rendered
    assert "Banked resets: 3 · next known expiry Jan 29, 12:00 UTC · 1 expiry unknown" in rendered
    assert "\n\nAPI balances\n\nOpenAI API\n  Available balance: $60.00" in rendered
    assert "Grant: $60.00 remaining · expires Jun 1" in rendered
    assert "Anthropic API\n  Shared org\n    Available balance: $75.00" in rendered
    assert "Promo grant: $25.00 remaining · expires May 1 · scope: Claude API (Sonnet)" in rendered
    assert "$100.00 granted" not in rendered
    window_rows = [line for line in rendered.splitlines() if "% used" in line]
    assert len({line.index("% used") for line in window_rows}) == 1
    assert len({line.index("↻") for line in window_rows}) == 1
    forecast_rows = [line for line in window_rows if "leaves" in line]
    assert len({line.index("leaves") for line in forecast_rows}) == 1


def test_details_show_grant_originals_full_deadlines_pace_and_peak_schedule(
    quota_fixture: Callable[[str], AllQuotas],
) -> None:
    quotas = quota_fixture("binding_summary")

    default = human.render(quotas, now=_NOW, tz=UTC)
    detailed = human.render(quotas, now=_NOW, tz=UTC, details=True)

    assert "pace +10 pp" not in default
    assert "3x windows (local)" not in default
    assert "pace +10 pp" in detailed
    assert "3x windows (local)" in detailed

    credits = human.render(quota_fixture("openai_platform_credits"), now=_NOW, tz=UTC, details=True)
    assert "expires 2026-02-01 00:00 UTC" in credits
    assert "$100.00 granted" in credits


def test_observed_account_has_compact_expiries_and_broad_promo_scope_only_in_details(
    quota_fixture: Callable[[str], AllQuotas],
) -> None:
    quotas = quota_fixture("observed_account_2026_11_04")
    default = human.render(quotas, now=quotas.fetched_at, tz=UTC)
    detailed = human.render(quotas, now=quotas.fetched_at, tz=UTC, details=True)

    assert "Promo grant:" in default
    assert "scope:" not in default
    assert "managed-agents-onprem" in detailed
    for date in ("Nov 25", "Dec 1", "Jan 1, 2027", "Feb 1, 2027", "Jul 1, 2027"):
        assert any(line.endswith(f"expires {date}") for line in default.splitlines())


def test_paid_mode_keeps_each_window_row_and_marks_actionable_state(quota_fixture: Callable[[str], AllQuotas]) -> None:
    rendered = human.render(quota_fixture("paid_credits"), now=_NOW, tz=UTC)

    assert "Codex\n  Paid credits: 2,746 credits" in rendered
    assert "Included quota exhausted · paid credits available" in rendered
    assert "5h: 100% used  ↻ 1h 30m" in rendered
    assert "7d:  38% used  ↻ 3d 14h" in rendered


def test_empty_success_has_precise_empty_state_and_does_not_render_burn_schedule(
    quota_fixture: Callable[[str], AllQuotas],
) -> None:
    rendered = human.render(quota_fixture("empty"), now=_NOW, tz=UTC)

    assert rendered.count("No quota or credit data available") == 3
    assert "burn" not in rendered
    assert "Claude\n  No quota or credit data available" in rendered


def test_offline_view_reports_cached_snapshot_age_and_hides_raw_error_by_default(
    quota_fixture: Callable[[str], AllQuotas],
) -> None:
    view = to_view(quota_fixture("binding_summary"), now=_NOW).model_copy(
        update={"connection_error": "request timed out", "fetched_at": _NOW - timedelta(minutes=8)}
    )

    default = human.render_view(view, now=_NOW, tz=UTC).plain
    detailed = human.render_view(view, now=_NOW, tz=UTC, details=True).plain

    assert default.startswith("Offline · cached snapshot from 8m ago")
    assert "request timed out" not in default
    assert "connection error: request timed out" in detailed


def test_provider_error_uses_shared_summary_and_details_keep_original_error(
    quota_fixture: Callable[[str], AllQuotas],
) -> None:
    rendered = human.render(quota_fixture("tints"), now=_NOW, tz=UTC)
    detailed = human.render(quota_fixture("tints"), now=_NOW, tz=UTC, details=True)

    assert "Claude\n  Authentication failed" in rendered
    assert "invalid_token" not in rendered
    assert "error: HTTP 401 invalid_request: invalid_token" in detailed


def test_stale_observation_is_marked_without_showing_pace_or_forecast(
    quota_fixture: Callable[[str], AllQuotas],
) -> None:
    rendered = human.render(quota_fixture("stale_observation"), now=_NOW, tz=UTC)

    assert "Stale data · last observed 20m ago" in rendered
    assert "pace " not in rendered
    assert "leaves" not in rendered


def test_expired_reset_waits_for_new_usage_and_suppresses_forecast(quota_fixture: Callable[[str], AllQuotas]) -> None:
    rendered = human.render(quota_fixture("reset_pending"), now=_NOW, tz=UTC)

    assert "Reset time passed · awaiting updated usage" in rendered
    assert "↻ awaiting update" in rendered
    assert "pace " not in rendered
    assert "leaves" not in rendered


@pytest.mark.parametrize(
    ("scenario", "default_visible", "details_visible"),
    [("ordinary", False, True), ("exhausted", True, True), ("pending", False, True), ("unknown", False, False)],
)
def test_banked_reset_visibility(
    quota_fixture: Callable[[str], AllQuotas], scenario: str, default_visible: bool, details_visible: bool
) -> None:
    quotas = quota_fixture(f"banked_resets_{scenario}")
    assert ("Banked resets: 0" in human.render(quotas, now=_NOW, tz=UTC)) == default_visible
    assert ("Banked resets: 0" in human.render(quotas, now=_NOW, tz=UTC, details=True)) == details_visible


def test_zero_resets_stop_showing_by_default_when_any_window_passes_its_reset(
    quota_fixture: Callable[[str], AllQuotas],
) -> None:
    quotas = quota_fixture("banked_resets_exhausted")
    assert "Banked resets: 0" in human.render(quotas, now=_NOW, tz=UTC)
    later = _NOW + timedelta(minutes=1)
    assert "Banked resets" not in human.render(quotas, now=later, tz=UTC)
    stale = human.render(quotas, now=_NOW + timedelta(minutes=11), tz=UTC, details=True)
    assert "Banked resets: 0" in stale
    assert "Stale data" in stale


def test_near_exhausted_usage_does_not_round_up_to_full_quota() -> None:
    quotas = _quota(FetchSuccess(windows=[QuotaWindow(used_percent=99.9, reset_seconds=3600, window_seconds=5 * 3600)]))

    assert "99% used" in human.render(quotas, now=_NOW, tz=UTC)
    assert "100% used" not in human.render(quotas, now=_NOW, tz=UTC)


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        (datetime(2026, 1, 29, 11, 59, 59, tzinfo=UTC), "Jan 29, 11:59 UTC"),
        (datetime(2026, 1, 29, 12, 0, 1, tzinfo=UTC), "Jan 29"),
        (datetime(2027, 1, 29, 12, 0, tzinfo=UTC), "Jan 29, 2027"),
    ],
)
def test_deadline_uses_local_time_and_date_only_after_two_weeks(when: datetime, expected: str) -> None:
    assert format_deadline(when, _NOW, UTC) == expected
    assert format_deadline(when, _NOW, UTC, details=True) == when.strftime("%Y-%m-%d %H:%M UTC")


def test_reset_credit_summary_is_compact_by_default_and_lists_all_expiries_in_details() -> None:
    expiries = [datetime(2026, 1, 20, 9, 0, tzinfo=UTC), datetime(2026, 2, 2, 12, 34, tzinfo=UTC)]

    assert format_reset_credit_summary(3, expiries, False, _NOW, UTC) == (
        "3 · next known expiry Jan 20, 09:00 UTC · 1 expiry unknown"
    )
    assert format_reset_credit_summary(3, expiries, False, _NOW, UTC, details=True) == (
        "3 · known expiries 2026-01-20 09:00 UTC, 2026-02-02 12:34 UTC · 1 expiry unknown"
    )
    assert format_reset_credit_summary(2, [], True, _NOW, UTC) == "2 · expiry lookup failed"
    assert format_reset_credit_summary(0, expiries, False, _NOW, UTC) == "0"


@pytest.mark.parametrize("fixture_name", FIXTURE_NAMES)
def test_renders_shared_fixture(
    quota_fixture: Callable[[str], AllQuotas], fixture_name: str, snapshot: SnapshotAssertion
) -> None:
    quotas = quota_fixture(fixture_name)
    assert human.render(quotas, now=quotas.fetched_at, tz=UTC) == snapshot


def test_terminal_window_keeps_shared_tint() -> None:
    quotas = _quota(FetchSuccess(windows=[QuotaWindow(used_percent=60, reset_seconds=9000, window_seconds=18000)]))

    rendered = human.render_text(quotas, now=_NOW, tz=UTC)

    assert "60% used" in rendered.plain
    assert any(span.style == "yellow" for span in rendered.spans)


def test_reset_expiry_time_uses_viewer_timezone() -> None:
    pacific = timezone(timedelta(hours=-8))
    expiry = datetime(2026, 1, 29, 5, 0, tzinfo=UTC)

    assert format_reset_credit_summary(1, [expiry], False, _NOW, pacific) == (
        "1 · next known expiry Jan 28, 21:00 UTC-08:00"
    )
