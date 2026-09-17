"""The rules for reading a presented bearer credential, wherever it arrives."""

from __future__ import annotations

import re
from collections.abc import Sequence

_BEARER = re.compile(r"Bearer +([A-Za-z0-9._~+/=-]+)", re.IGNORECASE)


def sole_header(values: Sequence[str]) -> str | None:
    """The one header value presented, or None where none or several were.

    Several is not one: a request carrying two credentials has not said which it stands on, and
    picking either would let a caller smuggle a second past whatever inspected the first.
    """
    return values[0] if len(values) == 1 else None


def parse_bearer(value: str) -> str | None:
    """The token in a `Bearer <token>` header value, or None where it is not one.

    The charset is RFC 6750's `token68`, which is what a projected ServiceAccount token is. A door
    that also admits credentials this service did not mint -- an OAuth access token, say -- has no
    business holding them to that, and defers to whatever issued them instead.
    """
    match = _BEARER.fullmatch(value)
    return match.group(1) if match is not None else None
