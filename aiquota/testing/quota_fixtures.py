"""Shared aiquota render test fixtures, validated as production quota models."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from aiquota.models import AllQuotas, FetchSuccess, ProviderFetch, ProviderQuota, QuotaWindow
from aiquota.render.view_model import AllQuotasView, to_view

FIXTURE_NAMES = (
    "empty",
    "banked_resets_ordinary",
    "banked_resets_exhausted",
    "banked_resets_pending",
    "banked_resets_unknown",
    "tints",
    "binding_summary",
    "zai_peak",
    "hot",
    "exhausted",
    "extra_enabled_not_burning",
    "extra_disabled_positive",
    "extra_spend_fallback",
    "multi_window_reset_pending",
    "stale_paid_signals",
    "stale_fallback",
    "paid_credits",
    "openai_platform_credits",
    "anthropic_platform_credits",
    "claude_prepaid_balance",
    "claude_prepaid_balance_unavailable",
    "claude_prepaid_balance_stale",
    "claude_reset_grants",
    "reset_credit_expiry_unavailable",
    "platform_credits_with_subscription_resets",
    "wire_fields",
    "reset_pending",
    "stale_observation",
    "observed_account_2026_11_04",
)


def load_quota_fixture(path: Path) -> AllQuotas:
    return AllQuotas.model_validate(yaml.safe_load(path.read_text()))


def meter_quota_view(used_percent: float) -> AllQuotasView:
    now = datetime(2026, 1, 15, 12, tzinfo=UTC)
    quotas = AllQuotas(
        fetched_at=now,
        providers=[
            ProviderQuota(
                provider="codex",
                last_output=ProviderFetch(
                    fetched_at=now,
                    result=FetchSuccess(
                        windows=[QuotaWindow(used_percent=used_percent, reset_seconds=9000, window_seconds=18000)]
                    ),
                ),
            )
        ],
    )
    return to_view(quotas, now)


def load_quota_view_fixture(path: Path, *, now: datetime | None = None) -> dict[str, Any]:
    """Return the same derived wire view used by `/v1/quotas` and the desktop daemon."""
    quotas = load_quota_fixture(path)
    return to_view(quotas, now if now is not None else quotas.fetched_at).model_dump(mode="json")
