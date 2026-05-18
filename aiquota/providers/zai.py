"""z.ai quota provider.

Fetches 5-hour and 7-day token limits from the z.ai monitor API.
Auth via API key read from a configurable file path.
"""

import logging
from datetime import UTC, datetime
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from aiquota.models import ProviderFetch, QuotaWindow

logger = logging.getLogger(__name__)

QUOTA_URL = "https://api.z.ai/api/monitor/usage/quota/limit"
API_TIMEOUT_SECS = 5.0
SHORT_WINDOW_SECS = 5 * 3600
LONG_WINDOW_SECS = 7 * 86400


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


def fetch(api_key_path: str | None = None) -> ProviderFetch:
    now = datetime.now(UTC)
    if not api_key_path:
        return ProviderFetch(error="no api key path configured", fetched_at=now)

    try:
        key = Path(api_key_path).expanduser().read_text().strip()
    except OSError as e:
        return ProviderFetch(error=str(e), fetched_at=now)
    if not key:
        return ProviderFetch(error="api key file is empty", fetched_at=now)

    try:
        resp = httpx.get(QUOTA_URL, headers={"Authorization": f"Bearer {key}"}, timeout=API_TIMEOUT_SECS)
        resp.raise_for_status()
        quota = _QuotaResponse.model_validate(resp.json())
    except Exception as e:
        return ProviderFetch(error=str(e), fetched_at=now)

    limits = quota.data.limits if quota.data else []
    short_limit = next((lim for lim in limits if lim.type == "TOKENS_LIMIT" and lim.unit == 3), None)
    long_limit = next((lim for lim in limits if lim.type == "TOKENS_LIMIT" and lim.unit == 6), None)
    short = _to_window(short_limit, SHORT_WINDOW_SECS)
    long = _to_window(long_limit, LONG_WINDOW_SECS)
    return ProviderFetch(short_window=short, long_window=long, fetched_at=now)
