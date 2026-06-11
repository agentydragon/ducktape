"""Alembic migration environment — online mode only.

Connection is injected programmatically via config.attributes["connection"].
Call ActionStorage.initialize() to run migrations; do not invoke alembic CLI directly.
"""

from alembic import context


def run_migrations() -> None:
    conn = context.config.attributes["connection"]
    context.configure(
        connection=conn,
        target_metadata=context.config.attributes.get("target_metadata"),
        render_as_batch=True,  # SQLite-safe; no-op in PostgreSQL
    )
    with context.begin_transaction():
        context.run_migrations()


run_migrations()
