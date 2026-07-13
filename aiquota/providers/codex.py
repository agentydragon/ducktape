"""Codex (OpenAI/ChatGPT) usage provider.

Fetches 5-hour and 7-day utilization from the Codex wham usage API.
Auth via ~/.codex/auth.json (file-based; Secret Service not available from CLI).
"""

import base64
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from atomicwrites import atomic_write
from pydantic import BaseModel, ConfigDict

from aiquota.models import FetchError, FetchSuccess, ProviderFetch, QuotaWindow
from aiquota.providers.base import Provider
from aiquota.providers.client import provider_client

logger = logging.getLogger(__name__)

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
TOKEN_URL = "https://auth.openai.com/oauth/token"
OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
API_TIMEOUT_SECS = 5.0
TOKEN_REFRESH_INTERVAL = timedelta(days=8)
TOKEN_EXPIRY_SKEW_SECS = 30


class CodexSettings(BaseModel):
    enabled: bool = True
    auth_path: Path = Path.home() / ".codex" / "auth.json"


class _AuthTokens(BaseModel):
    model_config = ConfigDict(extra="ignore")

    access_token: str | None = None
    refresh_token: str | None = None
    account_id: str | None = None


@dataclass
class _AuthState:
    path: Path
    raw: dict[str, Any]
    tokens: _AuthTokens


class _WindowData(BaseModel):
    model_config = ConfigDict(extra="ignore")

    used_percent: float
    limit_window_seconds: float
    reset_after_seconds: float = 0.0


class _RateLimit(BaseModel):
    model_config = ConfigDict(extra="ignore")

    primary_window: _WindowData | None = None
    secondary_window: _WindowData | None = None


class _AdditionalRateLimit(BaseModel):
    model_config = ConfigDict(extra="ignore")

    limit_name: str
    rate_limit: _RateLimit


class _UsageResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    rate_limit: _RateLimit | None = None
    additional_rate_limits: list[_AdditionalRateLimit] = []


class _TokenRefreshResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id_token: str | None = None
    access_token: str | None = None
    refresh_token: str | None = None


def _read_auth(path: Path) -> _AuthState | None:
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(raw, dict):
        return None
    tokens_raw = raw.get("tokens")
    if not isinstance(tokens_raw, dict):
        return None
    try:
        tokens = _AuthTokens.model_validate(tokens_raw)
    except ValueError:
        return None
    if not tokens.access_token:
        return None
    return _AuthState(path=path, raw=raw, tokens=tokens)


def _save_auth(auth: _AuthState) -> None:
    auth.path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(auth.raw, indent=2)
    with atomic_write(auth.path, overwrite=True, mode="w", encoding="utf-8") as f:
        f.write(data)


def _auth_changed(before: _AuthState, after: _AuthState | None) -> bool:
    if after is None:
        return False
    return (
        after.tokens.access_token != before.tokens.access_token
        or after.tokens.refresh_token != before.tokens.refresh_token
        or after.tokens.account_id != before.tokens.account_id
    )


def _decode_jwt_payload(jwt: str) -> dict[str, Any] | None:
    parts = jwt.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1]
    padding = "=" * ((4 - len(payload) % 4) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload + padding)
        value = json.loads(decoded)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _access_token_expired(access_token: str) -> bool:
    payload = _decode_jwt_payload(access_token)
    exp = payload.get("exp") if payload else None
    if not isinstance(exp, int | float):
        return False
    return exp <= datetime.now(UTC).timestamp() + TOKEN_EXPIRY_SKEW_SECS


def _parse_last_refresh(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = value
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    if "." in normalized:
        head, tail = normalized.split(".", 1)
        offset_start = max(tail.find("+"), tail.find("-"))
        if offset_start == -1:
            fraction, offset = tail, ""
        else:
            fraction, offset = tail[:offset_start], tail[offset_start:]
        normalized = f"{head}.{fraction[:6]}{offset}"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _auth_stale(auth: _AuthState) -> bool:
    access_token = auth.tokens.access_token
    if not access_token:
        return False
    if _access_token_expired(access_token):
        return True
    last_refresh = _parse_last_refresh(auth.raw.get("last_refresh"))
    return last_refresh is not None and datetime.now(UTC) - last_refresh > TOKEN_REFRESH_INTERVAL


async def _refresh_token(auth: _AuthState, client: httpx.AsyncClient) -> _AuthState | None:
    refresh_token = auth.tokens.refresh_token
    if not refresh_token:
        return None
    resp = await client.post(
        TOKEN_URL,
        json={"client_id": OAUTH_CLIENT_ID, "grant_type": "refresh_token", "refresh_token": refresh_token},
        headers={"Content-Type": "application/json"},
    )
    resp.raise_for_status()
    data = _TokenRefreshResponse.model_validate(resp.json())
    if not data.access_token:
        return None

    latest = _read_auth(auth.path)
    if _auth_changed(auth, latest):
        return latest

    tokens_raw = auth.raw.setdefault("tokens", {})
    if not isinstance(tokens_raw, dict):
        return None
    tokens_raw["access_token"] = data.access_token
    if data.id_token:
        tokens_raw["id_token"] = data.id_token
    if data.refresh_token:
        tokens_raw["refresh_token"] = data.refresh_token
    auth.raw["last_refresh"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    _save_auth(auth)
    return _read_auth(auth.path)


async def _refresh_or_reload(auth: _AuthState, client: httpx.AsyncClient) -> _AuthState | None:
    latest = _read_auth(auth.path)
    if _auth_changed(auth, latest):
        return latest
    candidate = latest or auth
    try:
        return await _refresh_token(candidate, client)
    except httpx.HTTPStatusError:
        latest_after_error = _read_auth(auth.path)
        if _auth_changed(candidate, latest_after_error):
            return latest_after_error
        raise


def _usage_headers(auth: _AuthState) -> dict[str, str]:
    headers: dict[str, str] = {
        "Authorization": f"Bearer {auth.tokens.access_token}",
        "User-Agent": "codex_cli_rs/0.125.0 (Linux; x86_64)",
    }
    if auth.tokens.account_id:
        headers["ChatGPT-Account-Id"] = auth.tokens.account_id
    return headers


async def _fetch_usage(auth: _AuthState, client: httpx.AsyncClient) -> _UsageResponse:
    resp = await client.get(USAGE_URL, headers=_usage_headers(auth))
    resp.raise_for_status()
    return _UsageResponse.model_validate(resp.json())


def _to_window(w: _WindowData | None, name: str | None = None, display: bool = True) -> QuotaWindow | None:
    if w is None or w.limit_window_seconds <= 0:
        return None
    reset_secs = max(0, w.reset_after_seconds)
    return QuotaWindow(
        name=name,
        display=display,
        used_percent=w.used_percent,
        reset_seconds=reset_secs,
        window_seconds=w.limit_window_seconds,
        reset_at=datetime.now(UTC) + timedelta(seconds=reset_secs),
    )


def _to_success(usage: _UsageResponse) -> FetchSuccess:
    windows: list[QuotaWindow | None] = []
    if usage.rate_limit:
        windows.extend((_to_window(usage.rate_limit.primary_window), _to_window(usage.rate_limit.secondary_window)))
    for additional in usage.additional_rate_limits:
        windows.extend(
            (
                _to_window(additional.rate_limit.primary_window, additional.limit_name, display=False),
                _to_window(additional.rate_limit.secondary_window, additional.limit_name, display=False),
            )
        )
    return FetchSuccess(windows=[window for window in windows if window])


class CodexProvider(Provider):
    name = "codex"

    def __init__(self, settings: CodexSettings, debug: bool = False) -> None:
        self.settings = settings
        self.debug = debug

    async def fetch(self) -> ProviderFetch:
        now = datetime.now(UTC)
        auth = _read_auth(self.settings.auth_path)
        if not auth:
            return ProviderFetch(fetched_at=now, result=FetchError(error="no codex auth found"))

        async with provider_client(self.name, self.debug, {USAGE_URL}, API_TIMEOUT_SECS) as client:
            if _auth_stale(auth):
                try:
                    auth = await _refresh_or_reload(auth, client)
                except Exception as e:
                    return ProviderFetch(fetched_at=now, result=FetchError.from_exception(e, "codex token refresh"))
                if not auth:
                    return ProviderFetch(fetched_at=now, result=FetchError(error="codex token refresh failed"))

            try:
                usage = await _fetch_usage(auth, client)
            except httpx.HTTPStatusError as e:
                if e.response.status_code != 401:
                    return ProviderFetch(fetched_at=now, result=FetchError.from_exception(e, "codex usage fetch"))
                try:
                    refreshed = await _refresh_or_reload(auth, client)
                    if not refreshed:
                        return ProviderFetch(fetched_at=now, result=FetchError(error="codex token refresh failed"))
                    usage = await _fetch_usage(refreshed, client)
                except Exception as refresh_error:
                    return ProviderFetch(
                        fetched_at=now, result=FetchError.from_exception(refresh_error, "codex token refresh")
                    )
            except Exception as e:
                return ProviderFetch(fetched_at=now, result=FetchError.from_exception(e, "codex usage fetch"))

        return ProviderFetch(fetched_at=now, result=_to_success(usage))
