"""View model shared by the CLI and the GNOME extension.

The extension and the CLI used to each carry their own copy of policy
decisions like "is the user currently burning extra usage" — and predictably
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

from aiquota.models import AllQuotas, ProviderFetch, ProviderQuota

# Same threshold as render/human.py — see _OVER_PLAN_PERCENT there for rationale.
_OVER_PLAN_PERCENT = 100.0

ExtraStatus = Literal["none", "informational", "active"]


class ProviderView(BaseModel):
    provider: str
    last_output: ProviderFetch
    last_success: ProviderFetch | None
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

    `extra_usage.is_enabled` only signals "feature enabled on the account",
    and `extra_usage.used_usd` is a cumulative monthly tally — neither says
    anything about "right now". The real signal is the 7d window being
    exhausted (every further call now hits the monthly bill).
    """
    if out.extra_usage is None or not out.extra_usage.is_enabled:
        return False
    return out.long_window is not None and out.long_window.used_percent >= _OVER_PLAN_PERCENT


def _extra_status(out: ProviderFetch) -> ExtraStatus:
    if currently_over_plan(out):
        return "active"
    if out.extra_usage is not None and out.extra_usage.is_enabled and out.extra_usage.used_usd > 0:
        return "informational"
    return "none"
