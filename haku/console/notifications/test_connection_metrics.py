"""The provider-connection refresh-health gauges, against the real migrated schema."""

from __future__ import annotations

import datetime
import uuid

import pytest
import pytest_bazel
from prometheus_client import REGISTRY
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from haku.console.database_schema import OAuthTokenState, ProviderConnection
from haku.console.notifications.connection_metrics import refresh_connection_metrics
from haku.console.oauth.provider_connection_registry import ProviderConnectionKind
from haku.console.operator_identity_store import PostgresOperatorIdentityStore

NOW = datetime.datetime(2026, 8, 5, 21, 0, tzinfo=datetime.UTC)


async def _connection(
    sessions: async_sessionmaker[AsyncSession],
    operator_id: uuid.UUID,
    *,
    name: str,
    failing_for: datetime.timedelta | None,
    count: int,
) -> None:
    started_at = NOW - failing_for if failing_for is not None else None
    async with sessions() as session:
        # Built through the `token_state` relationship rather than a raw token_state_id, mirroring
        # provider_connection.py's own construction — it is what orders the two inserts against the
        # composite FK.
        session.add(
            ProviderConnection(
                operator_id=operator_id,
                connection_name=name,
                provider_name="google",
                provider=ProviderConnectionKind.GOOGLE,
                created_at=NOW,
                token_state=OAuthTokenState(
                    token_state_id=uuid.uuid4(),
                    operator_id=operator_id,
                    created_at=NOW,
                    updated_at=NOW,
                    access_token="access",
                    token_type="Bearer",
                    refresh_failure_started_at=started_at,
                    refresh_failure_initial_kind="internal" if started_at else None,
                    refresh_failure_initial_message="TokenResponseError" if started_at else None,
                    refresh_failure_latest_at=NOW if started_at else None,
                    refresh_failure_latest_kind="internal" if started_at else None,
                    refresh_failure_latest_message="TokenResponseError" if started_at else None,
                    refresh_failure_count=count,
                    refresh_failure_action="reconnect" if started_at else None,
                ),
            )
        )
        await session.commit()


# Read through the registry rather than the Gauge object: it is the public API, and it is what a
# scrape actually sees. `None` therefore means "no such sample", which is the distinction
# test_a_healthy_connection_reports_zero_rather_than_being_omitted exists to pin down.
def _sample(metric: str, name: str) -> float | None:
    return REGISTRY.get_sample_value(metric, {"connection": name, "provider": "google"})


def _age(name: str) -> float | None:
    return _sample("haku_console_connection_refresh_failure_age_seconds", name)


def _attempts(name: str) -> float | None:
    return _sample("haku_console_connection_refresh_failure_attempts", name)


async def test_a_failing_connection_reports_how_long_it_has_been_failing(
    migrated_sessions: async_sessionmaker[AsyncSession], migrated_identity_store: PostgresOperatorIdentityStore
) -> None:
    operator_id = await migrated_identity_store.resolve_configured_external_user_key("connection-metrics-operator")
    await _connection(
        migrated_sessions, operator_id, name="google_mail", failing_for=datetime.timedelta(hours=40), count=160
    )
    await refresh_connection_metrics(migrated_sessions, now=NOW)
    assert _age("google_mail") == pytest.approx(40 * 3600)
    assert _attempts("google_mail") == 160


async def test_a_healthy_connection_reports_zero_rather_than_being_omitted(
    migrated_sessions: async_sessionmaker[AsyncSession], migrated_identity_store: PostgresOperatorIdentityStore
) -> None:
    """A gauge that vanishes on recovery leaves the alert firing on its last scraped value, and
    makes "this connection is healthy" indistinguishable from "no such connection"."""
    operator_id = await migrated_identity_store.resolve_configured_external_user_key("connection-metrics-operator")
    await _connection(migrated_sessions, operator_id, name="google_calendar", failing_for=None, count=0)
    await refresh_connection_metrics(migrated_sessions, now=NOW)
    assert _age("google_calendar") == 0
    assert _attempts("google_calendar") == 0


async def test_recovery_clears_a_previously_failing_gauge(
    migrated_sessions: async_sessionmaker[AsyncSession], migrated_identity_store: PostgresOperatorIdentityStore
) -> None:
    operator_id = await migrated_identity_store.resolve_configured_external_user_key("connection-metrics-operator")
    await _connection(
        migrated_sessions, operator_id, name="google_drive", failing_for=datetime.timedelta(hours=3), count=9
    )
    await refresh_connection_metrics(migrated_sessions, now=NOW)
    assert _age("google_drive") == pytest.approx(3 * 3600)

    async with migrated_sessions() as session:
        state = (await session.execute(select(OAuthTokenState))).scalars().one()
        state.refresh_failure_started_at = None
        state.refresh_failure_initial_kind = None
        state.refresh_failure_initial_message = None
        state.refresh_failure_latest_at = None
        state.refresh_failure_latest_kind = None
        state.refresh_failure_latest_message = None
        state.refresh_failure_action = None
        state.refresh_failure_count = 0
        await session.commit()

    await refresh_connection_metrics(migrated_sessions, now=NOW)
    assert _age("google_drive") == 0


if __name__ == "__main__":
    pytest_bazel.main()
