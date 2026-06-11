"""Basic outbound connectivity probe for the SessionStart hook.

Replaces the historical `auth_proxy` subsystem. Older Claude Code containers
required an egress proxy (HTTPS_PROXY with JWT credentials) for internet
access; the hook daemon extracted Anthropic's TLS inspection CA, built a
Java truststore, and started a UDS proxy for Bazel gRPC. Current containers
reach the internet with no env var setup or custom CA infrastructure —
whether via a transparent network-layer proxy, direct egress, or something
else, we don't try to distinguish; we just check that a known host is
reachable.

If this probe starts failing, a future container generation likely re-requires
explicit proxy configuration. Restore the `auth_proxy` subsystem from git
history — `git log --all -- devinfra/claude/auth_proxy/` finds the removal.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

# Known-reachable host for the probe. BuildBuddy is a required dependency for
# normal operation anyway — if it's unreachable, nothing else will work either.
_PROBE_URL = "https://remote.buildbuddy.io/"
_PROBE_TIMEOUT_SECONDS = 5.0


@dataclass
class ConnectivityOk:
    """Probe reached the target. Says nothing about how (direct vs. transparent proxy)."""


@dataclass
class ConnectivityFailed:
    """Probe could not reach the target — explicit proxy setup may be needed."""

    reason: str


ConnectivityResult = ConnectivityOk | ConnectivityFailed


async def check_connectivity() -> ConnectivityResult:
    """Probe outbound reachability to a known host. Logs outcome."""
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_SECONDS) as client:
            response = await client.get(_PROBE_URL)
    except httpx.HTTPError as e:
        reason = f"{type(e).__name__}: {e}"
        logger.warning("connectivity: probe to %s failed: %s", _PROBE_URL, reason)
        return ConnectivityFailed(reason=reason)

    logger.info("connectivity: probe to %s ok (HTTP %d)", _PROBE_URL, response.status_code)
    return ConnectivityOk()
