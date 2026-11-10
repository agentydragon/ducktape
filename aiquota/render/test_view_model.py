from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest_bazel

from aiquota.models import (
    AllQuotas,
    AnthropicPlatformCreditAmount,
    AnthropicPlatformCredits,
    AnthropicPlatformCreditValue,
    AnthropicPlatformOrganization,
    AnthropicPlatformProduct,
    AnthropicPlatformScope,
    AnthropicPlatformTranche,
    ClaudeResetGrant,
    ExtraSpend,
    FetchError,
    FetchSuccess,
    OpenAIPlatformCreditGrant,
    OpenAIPlatformCredits,
    PaidCredits,
    PrepaidBalanceAvailable,
    ProviderFetch,
    ProviderQuota,
    QuotaWindow,
    SuccessfulProviderFetch,
)
from aiquota.render.view_model import currently_over_plan, refresh_view, to_view

if __name__ == "__main__":
    pytest_bazel.main()


_NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)


def test_claude_reset_rows_group_counts_keep_scope_and_hide_expired() -> None:
    expiry = _NOW + timedelta(days=2)
    result = FetchSuccess(
        claude_reset_grants=[
            ClaudeResetGrant(label="Fable resets", remaining=1, original=1, expires_at=expiry, usable_now=False),
            ClaudeResetGrant(label="Fable resets", remaining=1, original=1, expires_at=expiry, usable_now=True),
            ClaudeResetGrant(label="Full resets", remaining=1, original=1, expires_at=expiry, paused=True),
            ClaudeResetGrant(label="Fable resets", remaining=0, original=1, expires_at=expiry),
            ClaudeResetGrant(label="Fable resets", remaining=1, original=1, expires_at=_NOW - timedelta(seconds=1)),
        ]
    )
    quotas = AllQuotas(
        fetched_at=_NOW,
        providers=[ProviderQuota(provider="claude", last_output=ProviderFetch(fetched_at=_NOW, result=result))],
    )
    view = to_view(quotas, _NOW).providers[0]
    assert [(grant.label, grant.remaining, grant.original, grant.status) for grant in view.grants] == [
        ("Fable resets", "2", "2", None),
        ("Full resets", "1", "1", "paused"),
    ]
    assert refresh_view(to_view(quotas, _NOW), expiry).providers[0].grants == []
    assert len(result.claude_reset_grants or []) == 5


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
    later = to_view(quota, _NOW + timedelta(minutes=5)).providers[0]
    assert (first.display_windows[0].pace, first.summary_tint, first.summary_index) == ("+10 pp", "warn", 0)
    assert (later.display_windows[0].pace, later.summary_tint) == ("+8 pp", "warn")
    assert (first.display_windows[0].reset, later.display_windows[0].reset) == ("2h 30m", "2h 25m")
    assert (first.display_windows[0].elapsed, later.display_windows[0].elapsed) == (0.5, 31 / 60)


def test_openai_platform_grants_filter_tracks_time_and_preserves_snapshot() -> None:
    def grant(used: int, expires: datetime | None, *, scheduled: bool = False) -> OpenAIPlatformCreditGrant:
        return OpenAIPlatformCreditGrant(
            granted_usd=Decimal(10),
            used_usd=Decimal(used),
            effective_at=_NOW - timedelta(days=1),
            expires_at=expires,
            is_scheduled=scheduled,
            is_held_for_prepaid_transition=False,
        )

    credits = OpenAIPlatformCredits(
        total_available_usd=Decimal(20),
        total_paid_available_usd=Decimal(20),
        grants=[
            grant(0, _NOW + timedelta(hours=1)),
            grant(1, _NOW),
            grant(10, None),
            grant(2, None),
            grant(10, None, scheduled=True),
        ],
    )
    success = FetchSuccess(openai_platform_credits=credits)
    quota = AllQuotas(
        fetched_at=_NOW,
        providers=[
            ProviderQuota(provider="openai_platform", last_output=ProviderFetch(fetched_at=_NOW, result=success))
        ],
    )
    first = to_view(quota, _NOW).providers[0]
    assert first.display_openai_platform_credits is not None
    assert first.display_openai_platform_credits.grants == [credits.grants[i] for i in (0, 3, 4)]
    assert isinstance(first.last_output.result, FetchSuccess)
    assert first.last_output.result.openai_platform_credits == credits

    quota.providers[0].last_success = SuccessfulProviderFetch(fetched_at=_NOW, result=success)
    quota.providers[0].last_output = ProviderFetch(fetched_at=_NOW, result=FetchError(error="offline"))
    stale = to_view(quota, _NOW + timedelta(hours=1)).providers[0]
    assert stale.display_openai_platform_credits is not None
    assert stale.display_openai_platform_credits.grants == [credits.grants[i] for i in (3, 4)]
    assert stale.last_success is not None
    assert stale.last_success.result.openai_platform_credits == credits


def test_display_selection_preserves_latest_check_and_snapshot_provenance() -> None:
    older = _NOW - timedelta(hours=1)
    old_result = FetchSuccess(windows=[QuotaWindow(used_percent=25, reset_seconds=60, window_seconds=3600)])
    old = SuccessfulProviderFetch(fetched_at=older, result=old_result)
    cases: list[tuple[FetchSuccess | FetchError, SuccessfulProviderFetch | None, str, datetime | None]] = [
        (FetchSuccess(available_reset_credits=0), old, "current", _NOW),
        (FetchSuccess(), old, "last_success", older),
        (FetchError(error="offline"), old, "last_success", older),
        (FetchError(error="offline"), None, "none", None),
        (FetchSuccess(paid_credits=PaidCredits(balance="1")), old, "current", _NOW),
    ]
    for result, prior, expected_source, expected_time in cases:
        pq = ProviderQuota(
            provider="codex", last_output=ProviderFetch(fetched_at=_NOW, result=result), last_success=prior
        )
        view = to_view(AllQuotas(providers=[pq], fetched_at=_NOW), _NOW).providers[0]
        assert (view.display.source, view.display.observed_at) == (expected_source, expected_time)
        assert view.last_output == pq.last_output
        assert view.display.result == (result if expected_source == "current" else old_result if prior else None)


def test_display_selection_handles_hidden_windows_and_credit_metadata() -> None:
    hidden = QuotaWindow(used_percent=25, reset_seconds=60, window_seconds=3600, display=False)
    result = FetchSuccess(windows=[hidden], available_reset_credits=0, reset_credit_expiry_lookup_failed=True)
    pq = ProviderQuota(provider="codex", last_output=ProviderFetch(fetched_at=_NOW, result=result))
    view = to_view(AllQuotas(providers=[pq], fetched_at=_NOW), _NOW).providers[0]
    assert view.display.source == "current"
    assert view.display.result == result
    assert view.display_windows == []
    assert view.display.result is not None
    assert view.display.result.available_reset_credits == 0
    assert view.display.result.reset_credit_expiry_lookup_failed


def test_display_selection_retains_prepaid_balance_with_its_own_timestamp() -> None:
    older = _NOW - timedelta(hours=1)
    balance = PrepaidBalanceAvailable(amount_minor=12345, exponent=2, currency="USD")
    prior = SuccessfulProviderFetch(fetched_at=older, result=FetchSuccess(prepaid_balance=balance))
    current = FetchSuccess(available_reset_credits=0)
    pq = ProviderQuota(
        provider="claude", last_output=ProviderFetch(fetched_at=_NOW, result=current), last_success=prior
    )
    view = to_view(AllQuotas(providers=[pq], fetched_at=_NOW), _NOW).providers[0]
    assert view.display.source == "current"
    assert view.display.prepaid_balance == balance
    assert view.display.prepaid_stale_since == older


def test_anthropic_platform_expiries_filter_without_losing_provider_response() -> None:
    def tranche(identifier: str, expires_at: datetime | None, amount_minor: int) -> AnthropicPlatformTranche:
        amount = AnthropicPlatformCreditValue(
            credits=AnthropicPlatformCreditAmount(amount_minor=amount_minor, exponent=2)
        )
        return AnthropicPlatformTranche(
            id=identifier,
            currency="USD",
            granted_at=_NOW - timedelta(days=1),
            expires_at=expires_at,
            granted=amount,
            remaining=amount,
            scope=AnthropicPlatformScope(kind="unscoped"),
        )

    credits = AnthropicPlatformCredits(
        organizations=[
            AnthropicPlatformOrganization(
                uuid="org-1",
                name="Main org",
                currency="USD",
                balance=AnthropicPlatformCreditValue(
                    credits=AnthropicPlatformCreditAmount(amount_minor=12345, exponent=2)
                ),
                tranches=[
                    tranche("current", _NOW + timedelta(hours=1), 5000),
                    tranche("at-boundary", _NOW, 2000),
                    tranche("expired", _NOW - timedelta(seconds=1), 1000),
                    tranche("empty", None, 0),
                ],
            )
        ]
    )
    success = FetchSuccess(anthropic_platform_credits=credits)
    quota = AllQuotas(
        fetched_at=_NOW,
        providers=[
            ProviderQuota(provider="anthropic_platform", last_output=ProviderFetch(fetched_at=_NOW, result=success))
        ],
    )

    first = to_view(quota, _NOW).providers[0]

    assert first.display_anthropic_platform_credits is not None
    assert [item.id for item in first.display_anthropic_platform_credits.organizations[0].tranches] == ["current"]
    assert isinstance(first.last_output.result, FetchSuccess)
    assert first.last_output.result.anthropic_platform_credits == credits

    later = to_view(quota, _NOW + timedelta(hours=1)).providers[0]
    assert later.display_anthropic_platform_credits is not None
    assert later.display_anthropic_platform_credits.organizations[0].tranches == []


def test_reset_boundary_keeps_last_usage_without_predicting_renewal() -> None:
    window = QuotaWindow(used_percent=100, reset_seconds=60, window_seconds=18000, reset_at=_NOW + timedelta(minutes=1))
    quotas = AllQuotas(
        fetched_at=_NOW, providers=[ProviderQuota(provider="codex", last_output=_fetch(short_window=window))]
    )
    refreshed = refresh_view(to_view(quotas, _NOW), _NOW + timedelta(minutes=2)).providers[0]
    shown = refreshed.display_windows[0]
    assert refreshed.summary_index == 0
    assert (shown.used, shown.exhausted, shown.reset_pending, shown.reset) == (100, True, True, "awaiting update")
    assert (shown.pace, shown.forecast) == (None, None)
    assert refreshed.status == "Reset time passed · awaiting updated usage"
    assert refreshed.display.result is not None
    assert refreshed.display.result.windows[0].reset_seconds == 60


def test_old_success_and_offline_views_suppress_forecasts_without_reselecting() -> None:
    window = QuotaWindow(used_percent=45, reset_seconds=7200, window_seconds=18000)
    quotas = AllQuotas(
        fetched_at=_NOW, providers=[ProviderQuota(provider="claude", last_output=_fetch(short_window=window))]
    )
    original = to_view(quotas, _NOW)
    old = refresh_view(original, _NOW + timedelta(minutes=11)).providers[0]
    assert old.display.source == "current"
    assert old.stale_since == _NOW
    assert (old.display_windows[0].pace, old.display_windows[0].forecast) == (None, None)
    offline = refresh_view(original, _NOW + timedelta(minutes=1), connection_error="offline")
    assert offline.connection_error == "offline"
    assert offline.providers[0].stale_since == _NOW
    assert offline.providers[0].display == original.providers[0].display
    assert offline.providers[0].display_windows[0].reset == "1h 59m"


def test_normalized_credit_rows_keep_pools_organizations_and_restrictions_separate() -> None:
    amount = AnthropicPlatformCreditValue(credits=AnthropicPlatformCreditAmount(amount_minor=12345, exponent=2))
    organization = AnthropicPlatformOrganization(
        uuid="org",
        name="Main org",
        currency="USD",
        balance=amount,
        promo_tranches=[
            AnthropicPlatformTranche(
                id="promo",
                currency="USD",
                granted_at=_NOW,
                expires_at=_NOW + timedelta(days=30),
                granted=amount,
                remaining=amount,
                scope=AnthropicPlatformScope(kind="restricted"),
            )
        ],
    )
    result = FetchSuccess(
        prepaid_balance=PrepaidBalanceAvailable(amount_minor=2575, exponent=2, currency="USD"),
        paid_credits=PaidCredits(balance="2745.759713"),
        anthropic_platform_credits=AnthropicPlatformCredits(organizations=[organization]),
    )
    provider = ProviderQuota(provider="claude", last_output=ProviderFetch(fetched_at=_NOW, result=result))
    view = to_view(AllQuotas(providers=[provider], fetched_at=_NOW), _NOW).providers[0]
    assert [(row.label, row.value, row.group) for row in view.balances] == [
        ("Prepaid balance", "$25.75", None),
        ("Paid credits", "2,746 credits", None),
        ("Available balance", "$123.45", "Main org"),
    ]
    assert [(row.label, row.remaining, row.scope, row.group) for row in view.grants] == [
        ("Promo grant", "$123.45", "restricted", "Main org")
    ]


def test_normalized_prepaid_balances_preserve_exact_minor_units_and_currency() -> None:
    for minor, exponent, currency, expected in (
        (123, 0, "USD", "$123"),
        (1234567, 3, "EUR", "1,234.567 EUR"),
        (-123, 2, "USD", "-$1.23"),
        (0, 2, "USD", "$0.00"),
    ):
        result = FetchSuccess(
            prepaid_balance=PrepaidBalanceAvailable(amount_minor=minor, exponent=exponent, currency=currency)
        )
        provider = ProviderQuota(provider="claude", last_output=ProviderFetch(fetched_at=_NOW, result=result))
        row = to_view(AllQuotas(providers=[provider], fetched_at=_NOW), _NOW).providers[0].balances[0]
        assert row.value == expected


def test_broad_api_promo_scope_is_detail_while_model_and_product_restrictions_remain_visible() -> None:
    amount = AnthropicPlatformCreditValue(credits=AnthropicPlatformCreditAmount(amount_minor=2500, exponent=2))
    scope = AnthropicPlatformScope(
        kind="scoped",
        applies_to=[
            AnthropicPlatformProduct(product=product)
            for product in ("agent_sdk", "api", "batch", "console", "managed-agents", "managed-agents-onprem")
        ],
    )
    tranche = AnthropicPlatformTranche(
        id="broad-api-promo", currency="USD", granted_at=_NOW, granted=amount, remaining=amount, scope=scope
    )
    result = FetchSuccess(
        anthropic_platform_credits=AnthropicPlatformCredits(
            organizations=[
                AnthropicPlatformOrganization(
                    uuid="test-org", name="Broad promo scope", balance=amount, promo_tranches=[tranche]
                )
            ]
        )
    )
    quotas = AllQuotas(
        fetched_at=_NOW,
        providers=[
            ProviderQuota(provider="anthropic_platform", last_output=ProviderFetch(fetched_at=_NOW, result=result))
        ],
    )
    broad = to_view(quotas, _NOW).providers[0].grants[0]
    assert broad.scope is None
    assert broad.scope_detail is not None
    assert "managed-agents-onprem" in broad.scope_detail

    scope.applies_to[1].model_family = "sonnet"
    restricted = to_view(quotas, _NOW).providers[0].grants[0]
    assert restricted.scope is not None
    assert "api (Sonnet)" in restricted.scope
    assert restricted.scope_detail is None

    scope.applies_to[1].model_family = None
    scope.applies_to.pop()
    assert to_view(quotas, _NOW).providers[0].grants[0].scope is not None


def test_unexhausted_usage_never_rounds_up_to_an_exhausted_display() -> None:
    provider = ProviderQuota(
        provider="codex",
        last_output=_fetch(
            short_window=QuotaWindow(used_percent=99.9, reset_seconds=3600, window_seconds=18000),
            paid_credits=PaidCredits(balance="20"),
        ),
    )
    view = to_view(AllQuotas(providers=[provider], fetched_at=_NOW), _NOW).providers[0]
    assert view.display_windows[0].used == 99
    assert not view.display_windows[0].exhausted
    assert not view.paid_credits_active


def test_error_summaries_keep_authentication_and_transport_failures_distinct() -> None:
    for error, summary in (
        ("no credentials found", "Credentials unavailable"),
        ("HTTP 401 invalid_token", "Authentication failed"),
        ("HTTP 503 upstream unavailable", "Refresh failed"),
    ):
        provider = ProviderQuota(
            provider="claude", last_output=ProviderFetch(fetched_at=_NOW, result=FetchError(error=error))
        )
        view = to_view(AllQuotas(providers=[provider], fetched_at=_NOW), _NOW).providers[0]
        assert (view.error_summary, view.error_detail) == (summary, error)
