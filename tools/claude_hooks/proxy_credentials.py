"""Proxy credential parsing and JWT expiry checking.

Pure functions for parsing proxy URLs and checking credential expiry.
Used by proxy_setup and bazel_wrapper.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import ParseResult, urlparse

import jwt


def parse_proxy_url(proxy_url: str) -> ParseResult:
    """Parse proxy URL, raising ValueError if invalid."""
    parsed = urlparse(proxy_url)
    if not parsed.hostname:
        raise ValueError(f"Could not parse host from {proxy_url}")
    return parsed


def build_upstream_uri(proxy: ParseResult) -> str:
    """Build URI with credentials for upstream proxy (legacy format).

    Uses #username:password suffix format.
    """
    host = proxy.hostname
    port = proxy.port or 80

    if proxy.username:
        password = proxy.password or ""
        return f"http://{host}:{port}#{proxy.username}:{password}"
    return f"http://{host}:{port}"


def get_jwt_expiry(jwt_token: str) -> datetime | None:
    """Parse JWT and return expiry time, or None if invalid/no exp claim."""
    try:
        payload = jwt.decode(jwt_token, options={"verify_signature": False})
        exp = payload.get("exp")
        return datetime.fromtimestamp(exp, tz=UTC) if exp else None
    except jwt.DecodeError:
        return None


@dataclass
class CredentialStatus:
    """Result of checking credential expiry. Stores only the expiry timestamp."""

    expiry: datetime | None


def check_credential_expiry(proxy_url: str) -> CredentialStatus:
    """Check credential expiry from proxy URL. Returns CredentialStatus with expiry time."""
    parsed = parse_proxy_url(proxy_url)
    if not parsed.password:
        return CredentialStatus(expiry=None)

    # Check if password looks like a JWT (may have jwt_ prefix)
    password = parsed.password.removeprefix("jwt_")  # Strip jwt_ prefix

    expiry = get_jwt_expiry(password)
    return CredentialStatus(expiry=expiry)
