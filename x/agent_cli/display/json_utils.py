"""JSON utilities for display handlers."""

from __future__ import annotations

import json
from typing import Any


def parse_json_or_none(s: str | None) -> Any | None:
    """Parse JSON string, returning None on error or empty input.

    Args:
        s: JSON string to parse, or None

    Returns:
        Parsed JSON data, or None if input is empty/None or parsing fails
    """
    if not s:
        return None
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return None
