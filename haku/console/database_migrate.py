"""Alembic migration runner for haku-console's database."""

from __future__ import annotations

import re
from collections.abc import Collection
from pathlib import Path
from typing import Any

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, text

from haku.console.database_schema import metadata

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# Arbitrary fixed key, unique to this lock's purpose (no meaning beyond that). With more
# than one haku-console replica, every pod runs migrations at startup — pg_advisory_xact_lock
# serializes them onto the connection's transaction (auto-released on commit/rollback) so two
# pods starting at once don't race to apply the same migration.
_MIGRATION_LOCK_KEY = 0x4B41_4B55  # "KAKU" in hex, close enough to "haku" to be memorable


type OperatorIdentitySeed = tuple[str, str]

_POSTGRES_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _normalized_identity_seeds(seeds: Collection[OperatorIdentitySeed]) -> tuple[OperatorIdentitySeed, ...]:
    normalized: set[OperatorIdentitySeed] = set()
    trust_by_external_key: dict[str, str] = {}
    for configured_trust_domain, stable_external_user_key in seeds:
        trust_domain = configured_trust_domain.strip()
        if not trust_domain or not stable_external_user_key.strip():
            raise ValueError("operator identity migration seeds must not be empty")
        existing_trust = trust_by_external_key.get(stable_external_user_key)
        if existing_trust is not None and existing_trust != trust_domain:
            raise ValueError("legacy operator keys must map to exactly one configured trust domain")
        trust_by_external_key[stable_external_user_key] = trust_domain
        normalized.add((trust_domain, stable_external_user_key))
    return tuple(sorted(normalized))


def _validated_fastmcp_oauth_state_table(table_name: str | None) -> str | None:
    if table_name is None:
        return None
    # PostgreSQLStore expects one unqualified table name. Keep that contract explicit here before
    # the migration uses the name as a quoted SQL identifier.
    if _POSTGRES_IDENTIFIER.fullmatch(table_name) is None:
        raise ValueError("FastMCP OAuth state table must be an unqualified PostgreSQL identifier")
    return table_name


def run_migrations_for_connection(
    conn: Any,
    *,
    operator_identity_seeds: Collection[OperatorIdentitySeed] = (),
    fastmcp_oauth_state_table: str | None = None,
) -> None:
    conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _MIGRATION_LOCK_KEY})
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.attributes["connection"] = conn
    cfg.attributes["target_metadata"] = metadata
    cfg.attributes["operator_identity_seeds"] = _normalized_identity_seeds(operator_identity_seeds)
    cfg.attributes["fastmcp_oauth_state_table"] = _validated_fastmcp_oauth_state_table(fastmcp_oauth_state_table)
    alembic_command.upgrade(cfg, "head")


def apply_migrations(
    database_url: str,
    *,
    operator_identity_seeds: Collection[OperatorIdentitySeed] = (),
    fastmcp_oauth_state_table: str | None = None,
) -> None:
    """Upgrade the haku-console database to head. The explicit startup step (app.main) and the tests
    call this once against the shared database — migrations are an ownership of the process, not a
    side effect of constructing a ledger/store."""
    engine = create_engine(database_url)
    try:
        with engine.begin() as conn:
            run_migrations_for_connection(
                conn,
                operator_identity_seeds=operator_identity_seeds,
                fastmcp_oauth_state_table=fastmcp_oauth_state_table,
            )
    finally:
        engine.dispose()
