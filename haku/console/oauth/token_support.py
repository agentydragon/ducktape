"""Shared token helpers for the console's OAuth connection flows.

Token expiry, freshness against a refresh skew, public-base-URL normalization, and
token-endpoint response parsing, extracted from ``mcp/operator_oauth`` so every console OAuth
flow shares one implementation and the stores cannot drift.
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Mapping
from typing import Any

import httpx
from mcp.client.auth.utils import handle_token_response_scopes
from mcp.shared.auth import OAuthToken
from pydantic import ValidationError

from haku.console.settings import Settings

# Refresh a little before expiry so a token handed to a tool call stays valid for the call.
REFRESH_SKEW = datetime.timedelta(seconds=60)
_ERROR_BODY_LIMIT = 512
_OAUTH_ERROR_FIELDS = ("error", "error_description", "error_uri")


class TokenResponseError(RuntimeError):
    """A safe, structured token-endpoint rejection or invalid success response."""

    def __init__(self, message: str, *, status_code: int, oauth_error: str | None, invalid_response: bool) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.oauth_error = oauth_error
        self.invalid_response = invalid_response


def token_expires_at(token: OAuthToken, now: datetime.datetime) -> datetime.datetime | None:
    if token.expires_in is None:
        return None
    return now + datetime.timedelta(seconds=token.expires_in)


def token_is_fresh(expires_at: datetime.datetime | None, now: datetime.datetime) -> bool:
    return expires_at is None or expires_at > now + REFRESH_SKEW


def public_base_url(settings: Settings) -> str:
    return settings.public_base_url.rstrip("/")


def token_request_error_message(*, label: str, request_error: httpx.RequestError, timeout_seconds: float) -> str:
    """Describe token-endpoint transport failures even when httpx's message is empty."""
    if isinstance(request_error, httpx.TimeoutException):
        return f"{label} timed out after {timeout_seconds:g} seconds"
    detail = str(request_error).strip()
    suffix = f": {detail}" if detail else ""
    return f"{label} request failed: {type(request_error).__name__}{suffix}"


def token_request_headers(headers: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build token-request headers that ask for the OAuth-standard JSON response.

    RFC 6749 section 5.1 specifies a JSON success response from the token endpoint. Sending an
    explicit ``Accept`` preference makes that expectation unambiguous for interoperable endpoints
    that retain a legacy default representation. Callers may add authentication or content-type
    headers, but cannot accidentally omit the response-format preference.
    """
    return {**(headers or {}), "Accept": "application/json"}


async def parse_token_response(response: httpx.Response, *, label: str) -> OAuthToken:
    if response.status_code != 200:
        detail, oauth_error = _oauth_error_detail(response)
        raise TokenResponseError(
            f"{label} failed: {response.status_code}{detail}",
            status_code=response.status_code,
            oauth_error=oauth_error,
            invalid_response=False,
        )
    try:
        return await handle_token_response_scopes(response)
    except ValidationError as e:
        raise TokenResponseError(
            f"{label} response was invalid: {e}",
            status_code=response.status_code,
            oauth_error=None,
            invalid_response=True,
        ) from e


def _oauth_error_detail(response: httpx.Response) -> tuple[str, str | None]:
    """Return bounded, useful token-endpoint diagnostics without echoing token fields.

    OAuth error responses are normally JSON. Only the RFC error fields are retained from
    structured responses, so a broken endpoint cannot make us log an access or refresh token.
    Non-JSON responses are necessarily less structured; retain a short, whitespace-normalized
    excerpt because reverse proxies commonly return the only useful failure reason as plain text.
    """
    try:
        payload: Any = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        excerpt = " ".join(response.text.split())[:_ERROR_BODY_LIMIT]
        return (f": {excerpt}" if excerpt else ""), None

    if not isinstance(payload, dict):
        return f": unexpected JSON {type(payload).__name__} response", None
    oauth_error = {
        field: value for field in _OAUTH_ERROR_FIELDS if isinstance((value := payload.get(field)), str) and value
    }
    if not oauth_error:
        return ": OAuth error response contained no standard error fields", None
    encoded = json.dumps(oauth_error, ensure_ascii=True, separators=(",", ":"))
    return f": {encoded[:_ERROR_BODY_LIMIT]}", oauth_error.get("error")
