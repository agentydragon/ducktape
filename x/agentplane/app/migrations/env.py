"""Alembic environment for the integration app's schema."""

from alembic import context

from x.agentplane.app.operator_sessions import Base as SessionBase
from x.agentplane.app.trajectory import Base

# This history's own stamp. The Action Service owns a second Alembic history, and the app's
# integration tests point both runners at one database; the default `alembic_version` would have
# each read the other's stamp and fail to locate the revision.
VERSION_TABLE = "alembic_version_app"

connection = context.config.attributes["connection"]
target_metadata = context.config.attributes.get("target_metadata", [Base.metadata, SessionBase.metadata])
context.configure(connection=connection, target_metadata=target_metadata, version_table=VERSION_TABLE)
with context.begin_transaction():
    context.run_migrations()
