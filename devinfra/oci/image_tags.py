"""Branch tags published by Ducktape CI and selected by Flux."""

import re
from datetime import datetime


def pinned_tag(now: datetime, commit: str, *, prefix: str = "devel") -> str:
    return f"{prefix}-{now:%Y%m%d%H%M%S}-{commit[:7]}"


def pinned_tag_pattern(prefix: str) -> str:
    """Unanchored so callers can match a whole tag or find one in Git history."""
    return re.escape(prefix) + r"-[0-9]{14}-[0-9a-f]{7}"
