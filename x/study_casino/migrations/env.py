"""Alembic migration environment for Study Casino.

Connection is injected programmatically by DocStore startup; do not rely on a
checked-in alembic.ini for production.
"""

from __future__ import annotations

from alembic import context

from x.study_casino.models import Base


def run_migrations() -> None:
    conn = context.config.attributes["connection"]
    context.configure(connection=conn, target_metadata=Base.metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


run_migrations()
