"""Proxy environment variable names used by claude.

These constants define the standard proxy environment variables that various
tools and runtimes recognize. Use these instead of hardcoding the strings.
"""

import base64
import os
from urllib.parse import urlparse

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


def normalize_proxy_url(proxy_url: str) -> tuple[str, dict[str, str]]:
    """Split credentials out of a proxy URL into an explicit Proxy-Authorization header.

    urllib3 v2 does not auto-send Proxy-Authorization on HTTPS CONNECT tunnels when
    credentials are embedded in the proxy URL. Callers using raw urllib3 (e.g. the
    kubernetes Python client) must pass the header explicitly.

    Returns (url_without_credentials, proxy_headers_dict).
    If the URL has no credentials, returns (url, {}).
    """
    parsed = urlparse(proxy_url)
    if not parsed.username:
        return proxy_url, {}
    if not parsed.hostname:
        raise ValueError(f"Invalid proxy URL {proxy_url!r}: has credentials but missing hostname")
    password = parsed.password or ""
    auth = base64.b64encode(f"{parsed.username}:{password}".encode()).decode()
    proxy_headers = {"Proxy-Authorization": f"Basic {auth}"}
    netloc = parsed.hostname if parsed.port is None else f"{parsed.hostname}:{parsed.port}"
    clean_url = parsed._replace(netloc=netloc).geturl()
    return clean_url, proxy_headers


def get_proxy_url(env: dict[str, str]) -> str | None:
    """Get the proxy URL from env dict, walking PROXY_ENV_VARS in priority order."""
    for var in PROXY_ENV_VARS:
        if value := env.get(var):
            return value
    return None


def get_upstream_proxy_url() -> str | None:
    """Get the upstream proxy URL from os.environ."""
    return get_proxy_url(dict(os.environ))
