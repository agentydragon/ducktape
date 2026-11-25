"""Applying one database's Alembic history from the image that owns it."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from tenacity import Retrying, before_sleep_log, retry_if_exception_type, stop_after_delay, wait_exponential

from util.alembic_upgrade import upgrade
from util.db_url import render_postgresql_url

# SQLAlchemy loads psycopg from the URL scheme; Gazelle cannot infer the runtime dependency.
# gazelle:include_dep @pypi//psycopg

logger = logging.getLogger(__name__)

# How long the first connection is retried before the migrator exits and the kubelet's restart back-off takes over.
CONNECT_BOUND = timedelta(minutes=2)


@dataclass(frozen=True)
class MigrationRunner:
    """An Alembic history and the lock that serializes its concurrent upgrades."""

    migrations_dir: Path
    lock_key: int

    def sync_url(self, database_url: str) -> str:
        return render_postgresql_url(database_url, "postgresql+psycopg")

    def apply(self, database_url: str) -> None:
        """A migrator often starts alongside its database, so every failure of the first connection is
        retried in process, each one logged, until `CONNECT_BOUND` has passed."""
        engine = create_engine(self.sync_url(database_url))
        try:
            first_connection = Retrying(
                stop=stop_after_delay(CONNECT_BOUND),
                wait=wait_exponential(multiplier=0.1, max=1),
                retry=retry_if_exception_type(OperationalError),
                before_sleep=before_sleep_log(logger, logging.WARNING),
                reraise=True,
            )(engine.connect)
            with first_connection as connection, connection.begin():
                connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": self.lock_key})
                upgrade(connection, self.migrations_dir)
        finally:
            engine.dispose()
