"""Alembic environment for haku-console's MCP approval database.

Connection is injected programmatically by ``haku.console.database_migrate``.
"""

from __future__ import annotations

from alembic import context

from haku.console.database_schema import metadata


def run_migrations() -> None:
    conn = context.config.attributes["connection"]
    target_metadata = context.config.attributes.get("target_metadata", metadata)
    context.configure(connection=conn, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


run_migrations()
