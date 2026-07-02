from __future__ import annotations

import html
import json
from typing import Any


def parse_inline_date(date_ref_data: str) -> str:
    """Returns ISO-formatted date string with timezone notation."""
    data: dict[str, Any] = json.loads(html.unescape(date_ref_data))
    date_str: str = str(data["dateTimeString"])  # ensure precise type for mypy
    timezone: str = str(data.get("timezone", "")) if data.get("timezone", "") else ""

    # Check if it's a date-only value (no time component)
    # Date-only formats: YYYY, YYYY-MM, YYYY-MM-DD, YYYY-Www
    if "T" not in date_str and "/" not in date_str:
        # Date-only values don't include timezone
        return date_str
    if "/" in date_str and timezone:
        # Date range - need to add timezone to each date
        dates = date_str.split("/")
        return f"{dates[0]}[{timezone}]/{dates[1]}[{timezone}]"
    # Single DateTime value
    return f"{date_str}[{timezone}]" if timezone else date_str
