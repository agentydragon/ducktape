"""Scoped, unpartitioned browser cookies for session imports."""

import datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator

from platform_sessions.http_types import CookieValue, HttpToken


class ScopedCookie(BaseModel):
    """Cookie with the scope and attributes needed to restore it in Chromium.

    Providers must validate their own allowed names, domains, paths and secure
    requirements. Partitioned cookies require a separate representation.
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)

    name: HttpToken
    value: CookieValue = Field(repr=False)
    domain: str
    path: str
    secure: bool
    httpOnly: bool  # noqa: N815 - Chromium storage-state field name
    expires: float
    sameSite: Literal["Strict", "Lax", "None"]  # noqa: N815 - Chromium storage-state field name

    @field_validator("domain")
    @classmethod
    def valid_domain(cls, value: str) -> str:
        if not value or not value.isascii() or any(ord(c) <= 32 or ord(c) == 127 for c in value):
            raise ValueError("Invalid cookie domain")
        return value

    @field_validator("path")
    @classmethod
    def valid_path(cls, value: str) -> str:
        if not value.startswith("/") or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("Invalid cookie path")
        return value

    @classmethod
    def from_har(cls, cookie: dict[str, Any]) -> Self:
        """Read HAR cookie metadata without inventing its domain or path."""
        if cookie.get("partitionKey") or cookie.get("_partitionKey"):
            raise ValueError("Partitioned cookies cannot be represented")
        expiry = cookie.get("expires")
        expires = -1.0
        if expiry:
            try:
                parsed = datetime.datetime.fromisoformat(expiry)
            except ValueError:
                parsed = parsedate_to_datetime(expiry)
            if parsed.tzinfo is None:
                raise ValueError("Cookie expiry needs a time zone")
            expires = parsed.timestamp()
        return cls(
            name=cookie["name"],
            value=cookie["value"],
            domain=cookie["domain"],
            path=cookie["path"],
            secure=cookie["secure"],
            httpOnly=bool(cookie.get("httpOnly", False)),
            expires=expires,
            sameSite=cookie.get("sameSite") or "Lax",
        )
