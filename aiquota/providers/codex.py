"""Codex (OpenAI/ChatGPT) usage provider.

Fetches 5-hour and 7-day utilization from the Codex wham usage API.
Auth via ~/.codex/auth.json (file-based; Secret Service not available from CLI).
"""

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict

from aiquota.models import FetchError, FetchSuccess, ProviderFetch, QuotaWindow
from aiquota.providers.base import Provider

logger = logging.getLogger(__name__)

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
API_TIMEOUT_SECS = 5.0


class CodexSettings(BaseModel):
    enabled: bool = True
    auth_path: Path = Path.home() / ".codex" / "auth.json"


class _AuthTokens(BaseModel):
    model_config = ConfigDict(extra="ignore")

    access_token: str
    account_id: str | None = None


class _AuthFile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tokens: _AuthTokens | None = None


class _WindowData(BaseModel):
    model_config = ConfigDict(extra="ignore")

    used_percent: float
    limit_window_seconds: float
    reset_after_seconds: float = 0.0


class _RateLimit(BaseModel):
    model_config = ConfigDict(extra="ignore")

    primary_window: _WindowData | None = None
    secondary_window: _WindowData | None = None


class _UsageResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    rate_limit: _RateLimit | None = None


def _read_auth(path: Path) -> _AuthTokens | None:
    try:
        auth = _AuthFile.model_validate_json(path.read_text())
    except (OSError, ValueError):
        return None
    return auth.tokens


def _to_window(w: _WindowData | None) -> QuotaWindow | None:
    if w is None or w.limit_window_seconds <= 0:
        return None
    reset_secs = max(0, w.reset_after_seconds)
    return QuotaWindow(
        used_percent=w.used_percent,
        reset_seconds=reset_secs,
        window_seconds=w.limit_window_seconds,
        reset_at=datetime.now(UTC) + timedelta(seconds=reset_secs),
    )


class CodexProvider(Provider):
    name = "codex"

    def __init__(self, settings: CodexSettings) -> None:
        self.settings = settings

    def fetch(self) -> ProviderFetch:
        now = datetime.now(UTC)
        auth = _read_auth(self.settings.auth_path)
        if not auth:
            return ProviderFetch(fetched_at=now, result=FetchError(error="no codex auth found"))

        headers: dict[str, str] = {
            "Authorization": f"Bearer {auth.access_token}",
            "User-Agent": "codex_cli_rs/0.125.0 (Linux; x86_64)",
        }
        if auth.account_id:
            headers["ChatGPT-Account-Id"] = auth.account_id

        try:
            resp = httpx.get(USAGE_URL, headers=headers, timeout=API_TIMEOUT_SECS)
            resp.raise_for_status()
            usage = _UsageResponse.model_validate(resp.json())
        except Exception as e:
            return ProviderFetch(fetched_at=now, result=FetchError(error=str(e)))

        rl = usage.rate_limit
        short = _to_window(rl.primary_window if rl else None)
        long = _to_window(rl.secondary_window if rl else None)
        return ProviderFetch(fetched_at=now, result=FetchSuccess(short_window=short, long_window=long))
