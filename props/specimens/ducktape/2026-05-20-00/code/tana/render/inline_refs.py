from __future__ import annotations

import html
import json
import re
from collections.abc import Callable
from typing import Any

# Regex patterns for inline references
NODE_SPAN_PATTERN = re.compile(r'<span data-inlineref-node="([^"]+)"></span>')
DATE_SPAN_PATTERN = re.compile(r'<span data-inlineref-date="([^"]+)"></span>')


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


def replace_inline_refs(
    text: str, node_replacer: Callable[[str], str] | None = None, date_replacer: Callable[[str], str] | None = None
) -> str:
    """node_replacer takes node ID, date_replacer takes ISO date string."""
    if node_replacer:
        text = NODE_SPAN_PATTERN.sub(lambda m: node_replacer(m.group(1)), text)

    if date_replacer:

        def date_sub(m):
            iso_date = parse_inline_date(m.group(1))
            return date_replacer(iso_date)

        text = DATE_SPAN_PATTERN.sub(date_sub, text)

    return text


def find_inline_node_refs(text: str) -> list[str]:
    return NODE_SPAN_PATTERN.findall(text)
