"""Shared Electric role setup for Agentplane PostgreSQL tests."""

from time import monotonic, sleep

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

# The URL uses the psycopg dialect, which SQLAlchemy loads at runtime.
# gazelle:include_dep @pypi//psycopg

_CREATE_ELECTRIC_ROLE = text(
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'electric') "
    "THEN CREATE ROLE electric LOGIN REPLICATION PASSWORD 'electric'; END IF; END $$"
)


def ensure_electric_role(admin_url: str, *, ready_timeout_s: float = 0.0) -> None:
    """Create the cluster-wide login before migrations; publication grants remain migration-owned."""
    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT", connect_args={"connect_timeout": 2})
    deadline = monotonic() + ready_timeout_s
    try:
        while True:
            try:
                with engine.connect() as connection:
                    if ready_timeout_s > 0:
                        connection.execute(text("SELECT 1"))
                    connection.execute(_CREATE_ELECTRIC_ROLE)
                return
            except OperationalError:
                if ready_timeout_s <= 0 or monotonic() >= deadline:
                    raise
                sleep(0.1)
    finally:
        engine.dispose()
