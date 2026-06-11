"""Background upstream health probe for the OAuth facade.

The facade's process liveness (`/healthz`) says nothing about whether the
upstream MCP server is actually reachable and serving tools. A facade can be
"up" while exposing zero tools because the upstream rejects the server-held
bearer token — the recurring Tana failure mode, where the desktop renderer's
`validateToken` starts refusing the PAT while `/health` still reports healthy.

This module periodically lists the upstream's tools through the same transport
the proxy uses, exports the result as Prometheus metrics (so Grafana/Alertmanager
can see it), and feeds a readiness signal so the pod goes NotReady when the
upstream is dead instead of silently serving an empty tool list.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from fastmcp.client import Client
from prometheus_client import Gauge

from mcp_infra.oauth_facade.config import FacadeSettings
from mcp_infra.oauth_facade.proxy import build_transport

logger = logging.getLogger(__name__)

_UP = Gauge("mcp_facade_upstream_up", "1 if the last upstream tools/list probe succeeded, else 0", ["facade"])
_TOOLS = Gauge("mcp_facade_upstream_tools", "Number of tools advertised by the upstream MCP server", ["facade"])
_LAST_SUCCESS = Gauge(
    "mcp_facade_upstream_last_success_timestamp_seconds",
    "Unix timestamp of the last successful upstream tools/list probe",
    ["facade"],
)


@dataclass
class ProbeState:
    """Latest upstream-probe result, shared between the probe loop and HTTP handlers."""

    facade_name: str
    max_staleness_seconds: float
    last_success_monotonic: float | None = None
    last_success_tools: int = 0

    def record_success(self, tool_count: int) -> None:
        self.last_success_monotonic = time.monotonic()
        self.last_success_tools = tool_count
        _UP.labels(self.facade_name).set(1)
        _TOOLS.labels(self.facade_name).set(tool_count)
        _LAST_SUCCESS.labels(self.facade_name).set(time.time())

    def record_failure(self) -> None:
        # Leave last_success_* untouched: readiness is staleness-based, so a
        # single transient failure does not immediately flip the pod NotReady,
        # but the up=0 metric surfaces the failure to alerting right away.
        _UP.labels(self.facade_name).set(0)
        _TOOLS.labels(self.facade_name).set(0)

    def ready(self) -> bool:
        """True iff a probe recently succeeded with at least one tool."""
        if self.last_success_monotonic is None or self.last_success_tools <= 0:
            return False
        return (time.monotonic() - self.last_success_monotonic) <= self.max_staleness_seconds


async def _probe_once(settings: FacadeSettings) -> int:
    """List upstream tools through the proxy's transport; return the tool count."""
    async with Client(build_transport(settings)) as client:
        return len(await client.list_tools())


async def run_probe_loop(settings: FacadeSettings, state: ProbeState) -> None:
    """Periodically refresh `state` from the upstream until cancelled."""
    while True:
        try:
            tool_count = await _probe_once(settings)
            state.record_success(tool_count)
            logger.debug("upstream probe ok: %d tools", tool_count)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The loop must survive any upstream error (auth rejection, network
            # blip, upstream restart); the failure IS the signal we record and
            # alert on, so we log it loudly and continue rather than crashing.
            logger.warning("upstream probe failed", exc_info=True)
            state.record_failure()
        await asyncio.sleep(settings.probe_interval_seconds)
