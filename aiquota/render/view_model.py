"""View model shared by the CLI and the GNOME extension.

The extension and the CLI used to each carry their own copy of policy
decisions like "is the user currently burning extra spend" — and predictably
drifted (see aiquota/AGENTS.md). This module is the single source of truth
for those derived booleans; the GNOME extension consumes them via the
`aiquota gnome-extension-json` subcommand instead of re-deriving locally.

String formatting that depends on a live countdown (reset times, pace,
forecasts) stays on the extension side so the popup can tick once per
second without re-spawning the CLI.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from aiquota.models import AllQuotas, FetchSuccess, ProviderFetch, ProviderQuota, SuccessfulProviderFetch

# Same threshold as render/human.py — see _OVER_PLAN_PERCENT there for rationale.
_OVER_PLAN_PERCENT = 100.0

ExtraStatus = Literal["none", "informational", "active"]


class ProviderView(BaseModel):
    provider: str
    last_output: ProviderFetch
    last_success: SuccessfulProviderFetch | None
    currently_over_plan: bool
    extra_status: ExtraStatus


class AllQuotasView(BaseModel):
    fetched_at: datetime
    providers: list[ProviderView]


def to_view(quotas: AllQuotas) -> AllQuotasView:
    return AllQuotasView(fetched_at=quotas.fetched_at, providers=[_provider_view(pq) for pq in quotas.providers])


def _provider_view(pq: ProviderQuota) -> ProviderView:
    return ProviderView(
        provider=pq.provider,
        last_output=pq.last_output,
        last_success=pq.last_success,
        currently_over_plan=currently_over_plan(pq.last_output),
        extra_status=_extra_status(pq.last_output),
    )


def currently_over_plan(out: ProviderFetch) -> bool:
    """True when the user is actively paying USD above subscription right now.

    `ExtraSpend.is_enabled` only signals "feature enabled on the account",
    and `ExtraSpend.used_usd` is a cumulative monthly tally — neither says
    anything about "right now". The real signal is *any* rate-limit window
    being exhausted (every further call now hits the monthly bill).
    """
    if not isinstance(out.result, FetchSuccess):
        return False
    extra = out.result.extra_spend
    if extra is None or not extra.is_enabled:
        return False
    return any(window.used_percent >= _OVER_PLAN_PERCENT for window in out.result.windows)


def _extra_status(out: ProviderFetch) -> ExtraStatus:
    if currently_over_plan(out):
        return "active"
    if not isinstance(out.result, FetchSuccess):
        return "none"
    extra = out.result.extra_spend
    if extra is not None and extra.is_enabled and extra.used_usd > 0:
        return "informational"
    return "none"
