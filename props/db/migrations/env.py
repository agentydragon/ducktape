from logging.config import fileConfig

from alembic import context

# Import models for autogenerate support
from props.db.config import get_database_config
from props.db.models import Base

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add your model's MetaData object here
# for 'autogenerate' support
target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    # Get URL from config if provided, otherwise get from environment
    url = config.get_main_option("sqlalchemy.url")
    if url is None:
        db_config = get_database_config()
        url = db_config.admin_url()

    context.configure(
        url=url, target_metadata=target_metadata, literal_binds=True, dialect_opts={"paramstyle": "named"}
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    Expects a connection to be passed via config.attributes['connection'].
    This is set by session.py's _create_schema() for programmatic usage.

    CLI usage (alembic upgrade head) is NOT supported - use session.py instead.
    """
    # Get connection passed programmatically (e.g., from session.py)
    connection = config.attributes.get("connection", None)

    if connection is None:
        raise RuntimeError(
            "No connection provided to env.py. "
            "Alembic migrations must be run programmatically via session.py, not via CLI. "
            "Use: from props.db.session import init_db, recreate_database; init_db(); recreate_database()"
        )

    # Configure context with the provided connection
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
