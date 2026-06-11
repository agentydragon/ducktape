"""Client and models for the Claude subscription usage API."""

from datetime import datetime

import httpx
from pydantic import BaseModel, ConfigDict

# Undocumented Claude Code-specific endpoint for subscription utilization
# (5-hour / 7-day quotas). Not part of the official Anthropic Python SDK,
# which only exposes per-message token usage (anthropic.types.Usage).
_USAGE_API_URL = "https://api.anthropic.com/api/oauth/usage"
_API_TIMEOUT_SECONDS = 2.0


class UsageBucket(BaseModel):
    model_config = ConfigDict(extra="ignore")

    utilization: float
    resets_at: datetime | None = None


class ExtraUsage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    is_enabled: bool
    monthly_limit: float
    used_credits: float
    utilization: float
    currency: str
    disabled_reason: str | None = None


class UsageResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    five_hour: UsageBucket | None = None
    seven_day: UsageBucket | None = None
    seven_day_opus: UsageBucket | None = None
    seven_day_sonnet: UsageBucket | None = None
    extra_usage: ExtraUsage | None = None


def fetch_usage(token: str) -> UsageResponse:
    """Fetch subscription usage from the Claude API."""
    response = httpx.get(
        _USAGE_API_URL,
        headers={"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"},
        timeout=_API_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return UsageResponse.model_validate(response.json())
