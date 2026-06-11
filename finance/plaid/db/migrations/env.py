"""Alembic migration environment for finance.plaid.db.

Connection is injected programmatically via config.attributes["connection"].
Call PlaidLinkStorage.initialize() to run migrations; do not invoke alembic CLI directly.
"""

from alembic import context


def run_migrations() -> None:
    conn = context.config.attributes["connection"]
    context.configure(connection=conn, target_metadata=context.config.attributes.get("target_metadata"))
    with context.begin_transaction():
        context.run_migrations()


run_migrations()
