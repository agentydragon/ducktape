"""Claude subscription usage provider.

Fetches 5-hour and 7-day utilization from the undocumented Claude OAuth usage API,
with automatic token refresh via the platform token endpoint.
"""

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from aiquota.models import ExtraUsage, FetchError, FetchSuccess, ProviderFetch, QuotaWindow
from aiquota.providers.base import Provider

logger = logging.getLogger(__name__)

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_SCOPES = ["user:profile", "user:inference", "user:sessions:claude_code", "user:mcp_servers", "user:file_upload"]
SHORT_WINDOW_SECS = 5 * 3600
LONG_WINDOW_SECS = 7 * 86400
TOKEN_EXPIRY_SKEW_SECS = 30
API_TIMEOUT_SECS = 5.0


class ClaudeSettings(BaseModel):
    enabled: bool = True
    credentials_path: Path = Path.home() / ".claude" / ".credentials.json"


# Preserve unknown fields (e.g. mcpOAuth) on the round-trip so that
# _save_credentials after a token refresh doesn't clobber Claude Code's
# other state in ~/.claude/.credentials.json.
_CAMEL = ConfigDict(extra="allow", alias_generator=to_camel, populate_by_name=True)


class _OAuthTokens(BaseModel):
    model_config = _CAMEL

    access_token: str | None = None
    refresh_token: str | None = None
    expires_at: int | None = None


class _Credentials(BaseModel):
    model_config = _CAMEL

    claude_ai_oauth: _OAuthTokens | None = None


class _UsageBucket(BaseModel):
    model_config = ConfigDict(extra="ignore")

    utilization: float
    resets_at: str | None = None


# Discriminated union: the API returns explicit `null` for the numeric
# fields when extra-usage isn't entitled on the plan, so the only sane
# representation gates them behind `is_enabled`.
class _ExtraUsageDisabled(BaseModel):
    model_config = ConfigDict(extra="ignore")

    is_enabled: Literal[False] = False


class _ExtraUsageEnabled(BaseModel):
    model_config = ConfigDict(extra="ignore")

    is_enabled: Literal[True]
    monthly_limit: float
    used_credits: float
    utilization: float


_ExtraUsage = Annotated[_ExtraUsageEnabled | _ExtraUsageDisabled, Field(discriminator="is_enabled")]


class _UsageResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    five_hour: _UsageBucket | None = None
    seven_day: _UsageBucket | None = None
    extra_usage: _ExtraUsage | None = None


class _TokenRefreshResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    access_token: str | None = None
    refresh_token: str | None = None
    expires_in: float | None = None


def _read_credentials(path: Path) -> tuple[_Credentials, str | None]:
    try:
        raw = path.read_text()
    except OSError:
        return _Credentials(), None
    creds = _Credentials.model_validate_json(raw)
    oauth = creds.claude_ai_oauth
    token = oauth.access_token if oauth else None
    return creds, token


def _save_credentials(path: Path, creds: _Credentials) -> None:
    try:
        path.write_text(creds.model_dump_json(indent=2, by_alias=True))
    except OSError:
        logger.debug("Could not write Claude credentials", exc_info=True)


async def _refresh_token(path: Path, creds: _Credentials, client: httpx.AsyncClient) -> str | None:
    oauth = creds.claude_ai_oauth
    if not oauth or not oauth.refresh_token:
        return None
    resp = await client.post(
        TOKEN_URL,
        json={
            "grant_type": "refresh_token",
            "refresh_token": oauth.refresh_token,
            "client_id": OAUTH_CLIENT_ID,
            "scope": " ".join(OAUTH_SCOPES),
        },
    )
    resp.raise_for_status()
    data = _TokenRefreshResponse.model_validate(resp.json())
    if not data.access_token or data.expires_in is None:
        return None
    new_oauth = _OAuthTokens(
        access_token=data.access_token,
        refresh_token=data.refresh_token or oauth.refresh_token,
        expires_at=int(datetime.now(UTC).timestamp() * 1000 + data.expires_in * 1000),
    )
    creds.claude_ai_oauth = new_oauth
    _save_credentials(path, creds)
    return data.access_token


def _token_expired(creds: _Credentials) -> bool:
    oauth = creds.claude_ai_oauth
    if not oauth or not oauth.expires_at:
        return True
    return oauth.expires_at - datetime.now(UTC).timestamp() * 1000 <= TOKEN_EXPIRY_SKEW_SECS * 1000


def _to_window(bucket: _UsageBucket | None, window_secs: float) -> QuotaWindow | None:
    if bucket is None:
        return None
    reset_at: datetime | None = None
    reset_secs = 0.0
    if bucket.resets_at:
        try:
            reset_at = datetime.fromisoformat(bucket.resets_at)
            reset_secs = max(0, (reset_at.timestamp() - datetime.now(UTC).timestamp()))
        except (ValueError, OSError):
            pass
    return QuotaWindow(
        used_percent=bucket.utilization, reset_seconds=reset_secs, window_seconds=window_secs, reset_at=reset_at
    )


class ClaudeProvider(Provider):
    name = "claude"

    def __init__(self, settings: ClaudeSettings) -> None:
        self.settings = settings

    async def fetch(self) -> ProviderFetch:
        now = datetime.now(UTC)
        path = self.settings.credentials_path
        creds, token = _read_credentials(path)
        if not token:
            return ProviderFetch(fetched_at=now, result=FetchError(error="no credentials found"))

        try:
            async with httpx.AsyncClient(timeout=API_TIMEOUT_SECS) as client:
                if _token_expired(creds):
                    token = await _refresh_token(path, creds, client)
                    if not token:
                        return ProviderFetch(fetched_at=now, result=FetchError(error="token refresh failed"))
                resp = await client.get(
                    USAGE_URL, headers={"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"}
                )
            resp.raise_for_status()
            usage = _UsageResponse.model_validate(resp.json())
        except Exception as e:
            return ProviderFetch(fetched_at=now, result=FetchError(error=str(e)))

        short = _to_window(usage.five_hour, SHORT_WINDOW_SECS)
        long = _to_window(usage.seven_day, LONG_WINDOW_SECS)
        extra: ExtraUsage | None = None
        if isinstance(usage.extra_usage, _ExtraUsageEnabled):
            eu = usage.extra_usage
            extra = ExtraUsage(
                is_enabled=True,
                monthly_limit_usd=eu.monthly_limit / 100,
                used_usd=eu.used_credits / 100,
                utilization=eu.utilization,
            )
        return ProviderFetch(
            fetched_at=now, result=FetchSuccess(short_window=short, long_window=long, extra_usage=extra)
        )
