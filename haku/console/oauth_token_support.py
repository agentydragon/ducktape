"""Shared token helpers for the console's OAuth connection flows.

Token expiry, freshness against a refresh skew, public-base-URL normalization, and
token-endpoint response parsing, extracted from ``mcp_operator_oauth`` so every console OAuth
flow shares one implementation and the stores cannot drift.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable

import httpx
from mcp.client.auth.utils import handle_token_response_scopes
from mcp.shared.auth import OAuthToken
from pydantic import ValidationError

from haku.console.config import Settings

# Refresh a little before expiry so a token handed to a tool call stays valid for the call.
REFRESH_SKEW = datetime.timedelta(seconds=60)


def token_expires_at(token: OAuthToken, now: datetime.datetime) -> datetime.datetime | None:
    if token.expires_in is None:
        return None
    return now + datetime.timedelta(seconds=token.expires_in)


def token_is_fresh(expires_at: datetime.datetime | None, now: datetime.datetime) -> bool:
    return expires_at is None or expires_at > now + REFRESH_SKEW


def public_base_url(settings: Settings) -> str:
    return settings.public_base_url.rstrip("/")


async def parse_token_response(
    response: httpx.Response, *, label: str, error: Callable[[str], Exception]
) -> OAuthToken:
    if response.status_code != 200:
        raise error(f"{label} failed: {response.status_code}")
    try:
        return await handle_token_response_scopes(response)
    except ValidationError as e:
        raise error(f"{label} response was invalid: {e}") from e
