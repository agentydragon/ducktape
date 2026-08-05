"""Prometheus gauges for provider-connection refresh health.

A linked Google connection whose refresh token stops working fails **silently**: the console keeps
serving, the connector keeps appearing in the settings list, and only an agent that happens to call
``get_mcp_server_status`` finds out. Three such outages in July/August 2026 were each caught by
Haku noticing mid-run rather than by anything alerting — the state lives in Postgres
(``OAuthTokenState.refresh_failure_*``) and nothing scraped it.

Deliberately an **age**, not a counter of attempts. The failure mode is *continuous* failure, and a
counter tells you a refresh failed at some point, not whether the connection is dead right now. Age
answers "how long has this been broken", which is exactly what an alert threshold wants.

The values are refreshed from the database inside the ``/metrics`` handler rather than by a
``prometheus_client`` collector: ``Collector.collect()`` is synchronous and the session factory is
async, and bridging that is more machinery than one indexed query per scrape is worth.
"""

from __future__ import annotations

import datetime

from prometheus_client import Gauge
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from haku.console.database_schema import OAuthTokenState, ProviderConnection

CONNECTION_REFRESH_FAILURE_AGE = Gauge(
    "haku_console_connection_refresh_failure_age_seconds",
    "Seconds since a provider connection's OAuth refresh first started failing; 0 when healthy.",
    labelnames=("connection", "provider"),
)

CONNECTION_REFRESH_FAILURE_ATTEMPTS = Gauge(
    "haku_console_connection_refresh_failure_attempts",
    "Consecutive failed refresh attempts for a provider connection; 0 when healthy.",
    labelnames=("connection", "provider"),
)


async def refresh_connection_metrics(db_sessions: async_sessionmaker, *, now: datetime.datetime | None = None) -> None:
    """Re-sample every provider connection's refresh health into the gauges.

    Every known connection gets a sample each scrape, including the healthy ones at 0 — a gauge
    that simply disappears when a connection recovers leaves the alert firing on the last scraped
    value until it ages out, and makes "is this connection healthy" indistinguishable from "does
    this connection exist".
    """
    moment = now or datetime.datetime.now(datetime.UTC)
    async with db_sessions() as session:
        rows = (
            await session.execute(
                select(
                    ProviderConnection.connection_name,
                    ProviderConnection.provider,
                    OAuthTokenState.refresh_failure_started_at,
                    OAuthTokenState.refresh_failure_count,
                ).join(OAuthTokenState, ProviderConnection.token_state_id == OAuthTokenState.token_state_id)
            )
        ).all()

    for connection_name, provider, failure_started_at, failure_count in rows:
        labels = (connection_name, str(provider))
        age = (moment - failure_started_at).total_seconds() if failure_started_at is not None else 0.0
        CONNECTION_REFRESH_FAILURE_AGE.labels(*labels).set(max(age, 0.0))
        CONNECTION_REFRESH_FAILURE_ATTEMPTS.labels(*labels).set(failure_count)
