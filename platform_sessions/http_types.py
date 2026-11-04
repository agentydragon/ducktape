"""Shared HTTP token and cookie value validation."""

from typing import Annotated

from pydantic import Field, StringConstraints

# HTTP header and cookie names use the same token characters.
HttpToken = Annotated[str, Field(pattern=r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")]

# Session cookies are serialized directly into a Cookie request header.
CookieValue = Annotated[str, StringConstraints(pattern=r"^[\x21-\x2B\x2D-\x3A\x3C-\x7E]+$")]
