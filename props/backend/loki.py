"""Query agent container logs from Loki.

Promtail ships every pod's stdout/stderr to Loki with standard stream labels
(`namespace`, `pod`, `container`, ...). Agent pods are named
`<agent_type>-<slug>-<agent_run_id[:8]>`, so a run's logs are the streams whose
`pod` label ends in the run-id's 8-char prefix. Container logs are not persisted
to the DB (only the run's status is) — this is how they're read back.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx

logger = logging.getLogger(__name__)

# In-cluster Loki read (SimpleScalable) — overridable for tests / other clusters.
DEFAULT_LOKI_URL = "http://loki-read.loki.svc.cluster.local:3100"
ENV_LOKI_URL = "PROPS_LOKI_URL"

_QUERY_TIMEOUT_S = 10.0
_MAX_LINES = 5000  # newest N lines (the tail, where failures show up)
_LOOKBACK = timedelta(days=14)


def loki_base_url() -> str:
    return os.environ.get(ENV_LOKI_URL, DEFAULT_LOKI_URL)


def _logql_for_run(run_id: UUID) -> str:
    # Anchored regex (Loki label matchers are fully anchored): the pod name ends
    # in `-<run_id[:8]>`. The 8 hex chars are unique enough across the window.
    return f'{{namespace="props",pod=~".+-{str(run_id)[:8]}"}}'


def parse_query_range(payload: dict) -> str:
    """Flatten a Loki query_range response into chronological log text."""
    entries: list[tuple[str, str]] = []
    for stream in payload.get("data", {}).get("result", []):
        entries.extend((ts, line) for ts, line in stream.get("values", []))
    entries.sort(key=lambda e: e[0])  # by nanosecond timestamp, ascending
    return "\n".join(line for _, line in entries)


async def fetch_run_logs(run_id: UUID, *, base_url: str | None = None) -> str:
    """Return an agent run's container logs (up to the most recent `_MAX_LINES`),
    in chronological order. Empty string if Loki has nothing for the run."""
    base = base_url or loki_base_url()
    end = datetime.now(UTC)
    params = {
        "query": _logql_for_run(run_id),
        "start": str(int((end - _LOOKBACK).timestamp() * 1e9)),
        "end": str(int(end.timestamp() * 1e9)),
        "limit": str(_MAX_LINES),
        "direction": "backward",  # newest first; we re-sort ascending for display
    }
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{base}/loki/api/v1/query_range", params=params, timeout=_QUERY_TIMEOUT_S)
        resp.raise_for_status()
        return parse_query_range(resp.json())
