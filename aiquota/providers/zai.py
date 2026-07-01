"""z.ai quota provider.

Fetches 5-hour and 7-day token limits from the z.ai monitor API.
Auth via API key from a configurable file path, falling back to $ZAI_API_KEY
(so the z-claude wrapper, which sets the key in the env, works with no config).
"""

import logging
import os
from datetime import UTC, datetime
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from aiquota.models import FetchError, FetchSuccess, ProviderFetch, QuotaWindow
from aiquota.providers.base import Provider

logger = logging.getLogger(__name__)

QUOTA_URL = "https://api.z.ai/api/monitor/usage/quota/limit"
API_TIMEOUT_SECS = 5.0
SHORT_WINDOW_SECS = 5 * 3600
LONG_WINDOW_SECS = 7 * 86400


class ZaiSettings(BaseModel):
    enabled: bool = True
    # No sensible default — user must point this at a file containing the key.
    api_key_path: Path | None = None


# z.ai monitor API uses camelCase (nextResetTime, ...).
_ZAI = ConfigDict(extra="ignore", alias_generator=to_camel, populate_by_name=True)


class _Limit(BaseModel):
    model_config = _ZAI

    type: str | None = None
    unit: int | None = None
    percentage: float | None = None
    next_reset_time: float | None = None


class _LimitData(BaseModel):
    model_config = _ZAI

    limits: list[_Limit] = []


class _QuotaResponse(BaseModel):
    model_config = _ZAI

    data: _LimitData | None = None


def _to_window(limit: _Limit | None, window_secs: float) -> QuotaWindow | None:
    if limit is None or limit.percentage is None:
        return None
    reset_at: datetime | None = None
    reset_secs = 0.0
    if limit.next_reset_time is not None:
        reset_at = datetime.fromtimestamp(limit.next_reset_time / 1000, UTC)
        reset_secs = max(0, (reset_at - datetime.now(UTC)).total_seconds())
    return QuotaWindow(
        used_percent=limit.percentage, reset_seconds=reset_secs, window_seconds=window_secs, reset_at=reset_at
    )


def _resolve_api_key(settings: ZaiSettings) -> str | None:
    """API key from the configured file, else $ZAI_API_KEY.

    z-claude puts the key in the environment rather than a file, so the env
    var lets it work with zero aiquota config. The file takes precedence.
    """
    path = settings.api_key_path
    if path is not None:
        try:
            key = path.expanduser().read_text().strip()
        except OSError:
            key = None
        if key:
            return key
    return os.environ.get("ZAI_API_KEY")


class ZaiProvider(Provider):
    name = "zai"

    def __init__(self, settings: ZaiSettings) -> None:
        self.settings = settings

    async def fetch(self) -> ProviderFetch:
        now = datetime.now(UTC)
        key = _resolve_api_key(self.settings)
        if not key:
            return ProviderFetch(
                fetched_at=now, result=FetchError(error="no z.ai API key (set api_key_path or ZAI_API_KEY)")
            )

        try:
            async with httpx.AsyncClient(timeout=API_TIMEOUT_SECS) as client:
                resp = await client.get(QUOTA_URL, headers={"Authorization": f"Bearer {key}"})
            resp.raise_for_status()
            quota = _QuotaResponse.model_validate(resp.json())
        except Exception as e:
            return ProviderFetch(fetched_at=now, result=FetchError.from_exception(e, "z.ai quota fetch"))

        limits = quota.data.limits if quota.data else []
        short_limit = next((lim for lim in limits if lim.type == "TOKENS_LIMIT" and lim.unit == 3), None)
        long_limit = next((lim for lim in limits if lim.type == "TOKENS_LIMIT" and lim.unit == 6), None)
        short = _to_window(short_limit, SHORT_WINDOW_SECS)
        long = _to_window(long_limit, LONG_WINDOW_SECS)
        return ProviderFetch(fetched_at=now, result=FetchSuccess(short_window=short, long_window=long))
