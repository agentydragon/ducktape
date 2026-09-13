from __future__ import annotations

import re

from pydantic.networks import AnyUrl

_URI_PATTERN = re.compile(r"^([^:]+://)(.*?)$")


def add_resource_prefix(uri: str | AnyUrl, prefix: str) -> str:
    """Add prefix to resource URI: protocol://path -> protocol://prefix/path."""
    uri_str = str(uri) if isinstance(uri, AnyUrl) else uri
    match = _URI_PATTERN.match(uri_str)
    if match:
        protocol, path = match.groups()
        return f"{protocol}{prefix}/{path}"
    return uri_str
