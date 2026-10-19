"""The Goose baseline equals the schema the Alembic history built, and takes over a database that history built."""

import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import uuid4

import pytest
import pytest_bazel
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

from agentplane.history_service.storage.database_migrate import RUNNER
from agentplane.testing.electric_role import ensure_electric_role
from util.bazel.runfiles import get_required_path, own_repo_rlocation
from util.testing.postgres import DatabaseNames, create_temporary_database

# SQLAlchemy loads this URL dialect dynamically.
# gazelle:include_dep @pypi//psycopg

_MIGRATOR = get_required_path(
    own_repo_rlocation("agentplane/history_service/migrations/cmd/history-migrate_/history-migrate")
)
_GOOSE_VERSION = "history_service_goose_version"
_ALEMBIC_HEAD = "0022_drop_pending_harness_stops"
_MIGRATOR_TABLES = ("alembic_version", _GOOSE_VERSION)

# Each query lists one kind of schema object, ordered by its columns, so two schemas compare as lists.
# Column order inside a table is not compared: no reader depends on it.
_CATALOG = {
    "columns": """
        SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull,
               pg_get_expr(d.adbin, d.adrelid), a.attidentity, a.attgenerated
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
        WHERE n.nspname = 'public' AND c.relkind = 'r' AND a.attnum > 0 AND NOT a.attisdropped
              AND c.relname <> ALL(:migrator_tables)
        ORDER BY 1, 2""",
    "constraints": """
        SELECT c.relname, con.conname, con.contype, pg_get_constraintdef(con.oid)
        FROM pg_constraint con
        JOIN pg_class c ON c.oid = con.conrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname <> ALL(:migrator_tables)
        ORDER BY 1, 2""",
    "indexes": """
        SELECT tablename, indexname, indexdef FROM pg_indexes
        WHERE schemaname = 'public' AND tablename <> ALL(:migrator_tables)
        ORDER BY 1, 2""",
    "sequences": """
        SELECT sequencename, data_type::text, start_value, min_value, max_value, increment_by, cycle
        FROM pg_sequences WHERE schemaname = 'public' AND sequencename NOT LIKE :migrator_sequences
        ORDER BY 1""",
    "replica_identity": """
        SELECT c.relname, c.relreplident FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relname <> ALL(:migrator_tables)
        ORDER BY 1""",
    "electric_grants": """
        SELECT table_name, privilege_type FROM information_schema.role_table_grants
        WHERE grantee = 'electric' AND table_schema = 'public' ORDER BY 1, 2
        """,
    "electric_schema_usage": "SELECT has_schema_privilege('electric', 'public', 'USAGE')",
    "publications": """
        SELECT p.pubname, p.puballtables, p.pubinsert, p.pubupdate, p.pubdelete, p.pubtruncate,
               array_agg(t.tablename ORDER BY t.tablename) FILTER (WHERE t.tablename IS NOT NULL)
        FROM pg_publication p LEFT JOIN pg_publication_tables t ON t.pubname = p.pubname
        GROUP BY p.pubname, p.puballtables, p.pubinsert, p.pubupdate, p.pubdelete, p.pubtruncate
        ORDER BY 1""",
}


def _catalog(engine: Engine) -> dict[str, list[tuple[object, ...]]]:
    parameters = {"migrator_tables": list(_MIGRATOR_TABLES), "migrator_sequences": f"{_GOOSE_VERSION}%"}
    with engine.connect() as conn:
        return {kind: [tuple(row) for row in conn.execute(text(query), parameters)] for kind, query in _CATALOG.items()}


def _wire_url(url: str) -> str:
    # The local testcontainer has no TLS; production URLs keep their own sslmode.
    return (
        make_url(url).set(drivername="postgresql", query={"sslmode": "disable"}).render_as_string(hide_password=False)
    )


def _migrate(url: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(_MIGRATOR)],
        env={"AGENTPLANE_HISTORY_SERVICE_DATABASE_URL": _wire_url(url)},
        text=True,
        capture_output=True,
        timeout=60,
        check=check,
    )


@pytest.fixture
def electric_role(postgres_admin_url: str) -> None:
    ensure_electric_role(postgres_admin_url)


@contextmanager
def _engine(url: str) -> Iterator[Engine]:
    engine = create_engine(url)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.mark.usefixtures("electric_role")
def test_baseline_matches_the_alembic_head(postgres_admin_url: str, database_names: DatabaseNames) -> None:
    with (
        create_temporary_database(postgres_admin_url, database_names()) as alembic_url,
        create_temporary_database(postgres_admin_url, database_names()) as goose_url,
    ):
        RUNNER.apply(alembic_url)
        _migrate(goose_url)
        with _engine(alembic_url) as alembic, _engine(goose_url) as goose:
            assert _catalog(goose) == _catalog(alembic)


@pytest.mark.usefixtures("electric_role")
def test_goose_leaves_an_alembic_database_and_its_rows_as_they_were(
    postgres_admin_url: str, database_names: DatabaseNames
) -> None:
    session_id = uuid4()
    with create_temporary_database(postgres_admin_url, database_names()) as url, _engine(url) as engine:
        RUNNER.apply(url)
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO session_history (id, last_cursor) VALUES (:id, 1)"), {"id": session_id})
            conn.execute(
                text("INSERT INTO session_event (session_id, cursor, payload) VALUES (:id, 1, '\\x00'::bytea)"),
                {"id": session_id},
            )
        before = _catalog(engine)
        _migrate(url)
        _migrate(url)
        assert _catalog(engine) == before
        with engine.connect() as conn:
            assert (
                conn.scalar(text("SELECT count(*) FROM session_event WHERE session_id = :id"), {"id": session_id}) == 1
            )
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == _ALEMBIC_HEAD
            assert conn.scalar(text(f"SELECT count(*) FROM {_GOOSE_VERSION} WHERE version_id = 1 AND is_applied")) == 1


@pytest.mark.usefixtures("electric_role")
def test_goose_refuses_an_alembic_database_behind_its_head(
    postgres_admin_url: str, database_names: DatabaseNames
) -> None:
    with create_temporary_database(postgres_admin_url, database_names()) as url, _engine(url) as engine:
        RUNNER.apply(url, "0021_drop_harness_stop_records")
        assert _migrate(url, check=False).returncode != 0
        with engine.connect() as conn:
            assert conn.scalar(text(f"SELECT count(*) FROM {_GOOSE_VERSION} WHERE version_id = 1 AND is_applied")) == 0


@pytest.mark.usefixtures("electric_role")
def test_concurrent_runners_serialize(postgres_admin_url: str, database_names: DatabaseNames) -> None:
    with create_temporary_database(postgres_admin_url, database_names()) as url, _engine(url) as engine:
        env = {"AGENTPLANE_HISTORY_SERVICE_DATABASE_URL": _wire_url(url)}
        runners = [
            subprocess.Popen([str(_MIGRATOR)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for _ in range(2)
        ]
        for runner in runners:
            stdout, stderr = runner.communicate(timeout=60)
            assert runner.returncode == 0, (stdout, stderr)
        with engine.connect() as conn:
            assert conn.scalar(text(f"SELECT count(*) FROM {_GOOSE_VERSION} WHERE version_id = 1 AND is_applied")) == 1


if __name__ == "__main__":
    pytest_bazel.main()
