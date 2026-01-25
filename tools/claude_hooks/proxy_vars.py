"""Proxy environment variable names used by claude_hooks.

These constants define the standard proxy environment variables that various
tools and runtimes recognize. Use these instead of hardcoding the strings.
"""

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
