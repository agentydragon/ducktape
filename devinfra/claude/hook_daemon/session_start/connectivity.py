"""Basic internet connectivity probe for the SessionStart hook.

Replaces the historical `auth_proxy` subsystem. Older Claude Code containers
required an egress proxy (HTTPS_PROXY with JWT credentials) for internet
access; the hook daemon extracted Anthropic's TLS inspection CA, built a
Java truststore, and started a UDS proxy for Bazel gRPC. Current containers
provide a transparent proxy at the network layer with the Anthropic CA
already installed in the system CA bundle — no env var setup or custom CA
infrastructure is needed.

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
    """Direct internet connectivity works."""


@dataclass
class ConnectivityFailed:
    """Direct internet probe failed — proxy setup may be needed."""

    reason: str


ConnectivityResult = ConnectivityOk | ConnectivityFailed


async def check_connectivity() -> ConnectivityResult:
    """Probe direct internet reachability. Logs outcome."""
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_SECONDS) as client:
            response = await client.get(_PROBE_URL)
    except httpx.HTTPError as e:
        reason = f"{type(e).__name__}: {e}"
        logger.warning("connectivity: direct probe to %s failed: %s", _PROBE_URL, reason)
        return ConnectivityFailed(reason=reason)

    logger.info("connectivity: direct probe to %s ok (HTTP %d)", _PROBE_URL, response.status_code)
    return ConnectivityOk()
