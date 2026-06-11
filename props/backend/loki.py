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

_QUERY_TIMEOUT_S = 30.0
_MAX_LINES = 5000  # newest N lines (the tail, where failures show up)
# Padding around the run's lifetime, to catch log-shipping lag at the edges.
LOG_WINDOW_MARGIN = timedelta(minutes=10)


def _as_utc(dt: datetime) -> datetime:
    """Treat a naive datetime (SQLAlchemy may return tz-naive UTC) as UTC."""
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def run_log_window(
    *, created_at: datetime, last_status_change: datetime, is_in_progress: bool, now: datetime
) -> tuple[datetime, datetime]:
    """The [start, end] to query a run's logs over: its lifetime plus a margin.

    Keeping this tight is what avoids the Loki split-query fan-out: a run that lived for
    seconds gets a ~20-minute window (a couple of 15m splits) instead of 14 days (>1000).
    For a still-running agent the end is `now` (it's still producing logs).
    """
    start = _as_utc(created_at) - LOG_WINDOW_MARGIN
    end = (now if is_in_progress else _as_utc(last_status_change)) + LOG_WINDOW_MARGIN
    return start, end


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


async def fetch_run_logs(run_id: UUID, *, start: datetime, end: datetime, base_url: str | None = None) -> str:
    """Return an agent run's container logs (up to the most recent `_MAX_LINES`),
    in chronological order. Empty string if Loki has nothing for the run.

    `start`/`end` bound the query to the run's lifetime. This matters: Loki splits a
    query_range into `split_queries_by_interval` (15m) sub-queries, so a multi-day window
    fans out into >1000 sub-queries and times out. The caller passes the run's
    created_at/exit time so the window stays tight (see get_run_logs).
    """
    base = base_url or loki_base_url()
    params = {
        "query": _logql_for_run(run_id),
        "start": str(int(start.timestamp() * 1e9)),
        "end": str(int(end.timestamp() * 1e9)),
        "limit": str(_MAX_LINES),
        "direction": "backward",  # newest first; we re-sort ascending for display
    }
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{base}/loki/api/v1/query_range", params=params, timeout=_QUERY_TIMEOUT_S)
        resp.raise_for_status()
        return parse_query_range(resp.json())
