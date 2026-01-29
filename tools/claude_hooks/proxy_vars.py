"""Proxy environment variable names used by claude_hooks.

These constants define the standard proxy environment variables that various
tools and runtimes recognize. Use these instead of hardcoding the strings.
"""

from __future__ import annotations

import os

# All proxy variables recognized by various tools (curl, yarn, global-agent, etc.)
PROXY_ENV_VARS = [
    "HTTPS_PROXY",
    "https_proxy",
    "HTTP_PROXY",
    "http_proxy",
    "GLOBAL_AGENT_HTTPS_PROXY",
    "GLOBAL_AGENT_HTTP_PROXY",
    "YARN_HTTPS_PROXY",
    "YARN_HTTP_PROXY",
]


def get_upstream_proxy_url() -> str | None:
    """Get the upstream proxy URL from environment.

    Walks PROXY_ENV_VARS in priority order and returns the first non-empty value.
    """
    for var in PROXY_ENV_VARS:
        if value := os.environ.get(var):
            return value
    return None
