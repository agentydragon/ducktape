"""Mako preprocessor that allows standard markdown headings in templates.

Mako treats ## at line start as a single-line comment, silently eating the line.
This preprocessor escapes ## (and ###, ####, etc.) so they pass through as markdown.

If you need a Mako comment, use <%doc>...</%doc> instead of ##.
"""

import re

_MD_HEADING_RE = re.compile(r"^(\s*)(#{2,})", re.MULTILINE)


def markdown_heading_preprocessor(text: str) -> str:
    """Escape markdown headings (##, ###, etc.) so Mako doesn't eat them."""
    return _MD_HEADING_RE.sub(lambda m: f"{m.group(1)}${{{m.group(2)!r}}}", text)
