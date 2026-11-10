"""Shared Electric role setup for Agentplane PostgreSQL tests."""

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import OperationalError
from tenacity import Retrying, retry_if_exception_type, stop_after_delay, wait_fixed

# The URL uses the psycopg dialect, which SQLAlchemy loads at runtime.
# gazelle:include_dep @pypi//psycopg

_CREATE_ELECTRIC_ROLE = text(
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'electric') "
    "THEN CREATE ROLE electric LOGIN REPLICATION PASSWORD 'electric'; END IF; END $$"
)


def _create_electric_role(engine: Engine) -> None:
    with engine.connect() as connection:
        connection.execute(_CREATE_ELECTRIC_ROLE)


def ensure_electric_role(admin_url: str, *, ready_timeout_s: float = 0.0) -> None:
    """Create the cluster-wide login before migrations; publication grants remain migration-owned.

    Connection failures are retried for `ready_timeout_s`, for a server that is still starting.
    """
    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT", connect_args={"connect_timeout": 2})
    try:
        Retrying(
            stop=stop_after_delay(ready_timeout_s),
            wait=wait_fixed(0.1),
            retry=retry_if_exception_type(OperationalError),
            reraise=True,
        )(_create_electric_role, engine)
    finally:
        engine.dispose()
