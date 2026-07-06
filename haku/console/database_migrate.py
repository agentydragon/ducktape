"""Alembic migration runner for haku-console's database."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig

from haku.console.database_schema import metadata

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def run_migrations_for_connection(conn: Any) -> None:
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.attributes["connection"] = conn
    cfg.attributes["target_metadata"] = metadata
    alembic_command.upgrade(cfg, "head")
