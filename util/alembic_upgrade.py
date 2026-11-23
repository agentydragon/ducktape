"""Run an Alembic revision on a transaction owned by the caller."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, MetaData


def upgrade(connection: Connection, scripts: Path, revision: str = "head", *, metadata: MetaData | None = None) -> None:
    config = Config()
    config.set_main_option("script_location", str(scripts))
    config.attributes["connection"] = connection
    if metadata is not None:
        config.attributes["target_metadata"] = metadata
    command.upgrade(config, revision)
