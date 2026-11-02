from datetime import UTC, datetime, timedelta

import pytest_bazel

from aiquota.models import (
    AllQuotas,
    ExtraSpend,
    FetchError,
    FetchSuccess,
    PaidCredits,
    PlatformCreditGrant,
    PlatformCredits,
    ProviderFetch,
    ProviderQuota,
    QuotaWindow,
    SuccessfulProviderFetch,
)
from aiquota.render.view_model import currently_over_plan, to_view

if __name__ == "__main__":
    pytest_bazel.main()


_NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)


def _fetch(
    short_window: QuotaWindow | None = None,
    long_window: QuotaWindow | None = None,
    extra_spend: ExtraSpend | None = None,
    paid_credits: PaidCredits | None = None,
) -> ProviderFetch:
    return ProviderFetch(
        fetched_at=_NOW,
        result=FetchSuccess(
            windows=[window for window in (short_window, long_window) if window],
            extra_spend=extra_spend,
            paid_credits=paid_credits,
        ),
    )


def test_feature_off_is_not_over_plan() -> None:
    # is_enabled=False (no credit card / opted out) → never over plan, no matter the 7d window.
    fetch = _fetch(
        long_window=QuotaWindow(used_percent=100, reset_seconds=0, window_seconds=604800),
        extra_spend=ExtraSpend(is_enabled=False, monthly_limit_usd=100, used_usd=0, utilization=0),
    )
    assert not currently_over_plan(fetch)


def test_short_window_exhausted_with_feature_on_is_over_plan() -> None:
    # 5h window exhausted (burning extra) but 7d window still has headroom.
    fetch = _fetch(
        short_window=QuotaWindow(used_percent=105, reset_seconds=1200, window_seconds=18000),
        long_window=QuotaWindow(used_percent=96, reset_seconds=86400, window_seconds=604800),
        extra_spend=ExtraSpend(is_enabled=True, monthly_limit_usd=4600, used_usd=3120, utilization=67),
    )
    assert currently_over_plan(fetch)


def test_extra_status_transitions() -> None:
    quotas = AllQuotas(
        providers=[
            # over plan
            ProviderQuota(
                provider="claude",
                last_output=_fetch(
                    long_window=QuotaWindow(used_percent=100, reset_seconds=86400, window_seconds=604800),
                    extra_spend=ExtraSpend(is_enabled=True, monthly_limit_usd=100, used_usd=50, utilization=50),
                ),
            ),
            # informational: feature on, money spent this month, but prepaid has room
            ProviderQuota(
                provider="codex",
                last_output=_fetch(
                    long_window=QuotaWindow(used_percent=5, reset_seconds=86400, window_seconds=604800),
                    extra_spend=ExtraSpend(is_enabled=True, monthly_limit_usd=100, used_usd=10, utilization=10),
                ),
            ),
            # none: no extra spend at all
            ProviderQuota(
                provider="zai",
                last_output=_fetch(long_window=QuotaWindow(used_percent=5, reset_seconds=86400, window_seconds=604800)),
            ),
            # none: feature on but nothing spent yet this month
            ProviderQuota(
                provider="opus",
                last_output=_fetch(
                    long_window=QuotaWindow(used_percent=5, reset_seconds=86400, window_seconds=604800),
                    extra_spend=ExtraSpend(is_enabled=True, monthly_limit_usd=100, used_usd=0, utilization=0),
                ),
            ),
        ],
        fetched_at=_NOW,
    )
    statuses = {pv.provider: pv.extra_status for pv in to_view(quotas).providers}
    assert statuses == {"claude": "active", "codex": "informational", "zai": "none", "opus": "none"}


def test_paid_credits_active_only_with_balance_and_exhausted_window() -> None:
    window = QuotaWindow(used_percent=100, reset_seconds=1200, window_seconds=18000)
    providers = [
        ProviderQuota(provider=name, last_output=_fetch(short_window=window, paid_credits=PaidCredits(balance=balance)))
        for name, balance in (("positive", "12.5"), ("empty", "0"))
    ]
    providers.append(ProviderQuota(provider="no_credits", last_output=_fetch(short_window=window)))
    statuses = {
        view.provider: view.paid_credits_active
        for view in to_view(AllQuotas(providers=providers, fetched_at=_NOW)).providers
    }
    assert statuses == {"positive": True, "empty": False, "no_credits": False}


def test_window_presentation_updates_from_one_snapshot() -> None:
    window = QuotaWindow(
        used_percent=60, reset_seconds=9000, window_seconds=18000, reset_at=_NOW + timedelta(hours=2, minutes=30)
    )
    quota = AllQuotas(
        fetched_at=_NOW, providers=[ProviderQuota(provider="claude", last_output=_fetch(short_window=window))]
    )
    first = to_view(quota, _NOW).providers[0]
    later = to_view(quota, _NOW + timedelta(hours=1)).providers[0]
    assert (first.display_windows[0].pace, first.summary_tint, first.summary_index) == ("+10%", "warn", 0)
    assert (later.display_windows[0].pace, later.summary_tint) == ("-10%", "cool")
    assert (first.display_windows[0].reset, later.display_windows[0].reset) == ("2h30m", "1h30m")
    assert (first.display_windows[0].elapsed, later.display_windows[0].elapsed) == (0.5, 0.7)


def test_platform_grants_filter_tracks_time_and_preserves_snapshot() -> None:
    def grant(used: int, expires: datetime | None, *, scheduled: bool = False) -> PlatformCreditGrant:
        return PlatformCreditGrant(
            granted_usd=10,
            used_usd=used,
            effective_at=_NOW - timedelta(days=1),
            expires_at=expires,
            is_scheduled=scheduled,
            is_held_for_prepaid_transition=False,
        )

    credits = PlatformCredits(
        total_available_usd=20,
        total_paid_available_usd=20,
        grants=[
            grant(0, _NOW + timedelta(hours=1)),
            grant(1, _NOW),
            grant(10, None),
            grant(2, None),
            grant(10, None, scheduled=True),
        ],
    )
    success = FetchSuccess(platform_credits=credits)
    quota = AllQuotas(
        fetched_at=_NOW,
        providers=[
            ProviderQuota(provider="openai_platform", last_output=ProviderFetch(fetched_at=_NOW, result=success))
        ],
    )
    first = to_view(quota, _NOW).providers[0]
    assert first.display_platform_credits is not None
    assert first.display_platform_credits.grants == [credits.grants[i] for i in (0, 3, 4)]
    assert first.last_output.result.platform_credits == credits

    quota.providers[0].last_success = SuccessfulProviderFetch(fetched_at=_NOW, result=success)
    quota.providers[0].last_output = ProviderFetch(fetched_at=_NOW, result=FetchError(error="offline"))
    stale = to_view(quota, _NOW + timedelta(hours=1)).providers[0]
    assert stale.display_platform_credits is not None
    assert stale.display_platform_credits.grants == [credits.grants[i] for i in (3, 4)]
    assert stale.last_success is not None
    assert stale.last_success.result.platform_credits == credits
