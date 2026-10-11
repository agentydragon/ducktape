"""PostgreSQL test database utilities for isolated databases and migrated templates.

For suites that need a migrated schema but isolated writable state per test, create and migrate one
template for the session, then clone it into a fresh database for each case. Import the shared
fixtures directly into the owning package conftest; keep this wiring local to the suites that need
Postgres so unrelated tests do not load Docker or migration dependencies.

```python
from util.testing.postgres_fixtures import (
    database_clones, database_names, migrated_database_fixtures, postgres_admin_url, postgres_container,
    postgres_image,
)


database_template, db_url = migrated_database_fixtures(SERVICE_RUNNER, drivername="postgresql+asyncpg")
```

`database_names` allocates safe, deterministic, unique names from the current pytest node id. Use it
directly when a test needs a name but manages database retention or teardown itself. `database_clones`
owns an `ExitStack` for the test and drops every clone after dependent fixtures finish; specify each
clone's SQLAlchemy driver explicitly. The shared `postgres_admin_url` uses psycopg, and callers can
use `make_url` to preserve its credentials and query parameters while changing drivers.
`postgres_container` uses the session-scoped `postgres_image` fixture, which defaults to PostgreSQL 18;
override it in a suite that needs another image, such as pgvector.

`migrated_database_fixtures` returns both fixtures: one session migration template and one fresh
clone URL per test. Bind both at module scope. `template_name` and `url_name` select explicit pytest
fixture names when composing multiple services; the SQL database name derives from the template
fixture name. For setup that also needs role creation,
a source template, or extensions, compose `migrated_database_template` inside an explicit fixture.
For application-specific initialization that is not a `MigrationRunner`, use
`PostgresDatabaseTemplate` directly and perform that initialization once on `template.url`. It also
supports `template=` when one test schema must extend another. Keep tests that verify starting from an
empty database or applying a specific old migration state on direct create/drop fixtures instead of
cloning a migrated template.

PostgreSQL refuses to clone a template while it has open connections. Let migration engines and
pools close before the first clone, and keep the template connection-free afterwards. Schema-level
objects and extensions are copied with the database. Cluster roles are shared by every database;
database-level settings and privileges should be applied explicitly to each clone when a test needs
them. For resources outside the database that must be removed first, such as an inactive logical
replication slot, compose a cleanup context inside the clone context. Put a service context inside
that when the service must stop before slot cleanup; context exit then proceeds service, slot, clone.

When a caller needs custom cleanup or may retain a database for debugging, use
`create_database_sync(admin_url, name, template=template.template_name)` directly and pair it with
`force_drop_database_sync` as appropriate.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
from hashlib import sha256

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from util.db_migrations import MigrationRunner

# The dialect is chosen by the admin_url scheme the caller passes (sync callers use
# the postgresql:// default -> psycopg2, async callers postgresql+asyncpg://);
# SQLAlchemy loads it at runtime, so gazelle cannot see either dependency.
# gazelle:include_dep @pypi//asyncpg
# gazelle:include_dep @pypi//psycopg
# gazelle:include_dep @pypi//psycopg2_binary

_TERMINATE_SQL = text(
    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :db_name AND pid <> pg_backend_pid()"
)

_SAFE_DB_NAME = re.compile(r"^[a-zA-Z0-9_]+$")


def database_name_for_test(node_id: str, *, counter: int | None = None) -> str:
    """Return a readable, collision-resistant PostgreSQL identifier for a pytest node id.

    ``counter`` lets one test allocate several independent databases without relying on randomness.
    """
    identity = node_id if counter is None else f"{node_id}_{counter}"
    readable = re.sub(r"[^a-zA-Z0-9_]+", "_", identity).strip("_") or "test"
    suffix = sha256(identity.encode()).hexdigest()[:10]
    return f"{readable[:52].rstrip('_')}_{suffix}"


class DatabaseNames:
    """Allocate safe, distinct PostgreSQL database names for one pytest node."""

    def __init__(self, node_id: str) -> None:
        self.node_id = node_id
        self._counter = 0

    def __call__(self) -> str:
        name = database_name_for_test(self.node_id, counter=self._counter)
        self._counter += 1
        return name


class DatabaseClones:
    """Own per-test template clones and drop them together when their pytest fixture ends."""

    def __init__(self, names: DatabaseNames) -> None:
        self._names = names
        self._stack = ExitStack()

    def __enter__(self) -> DatabaseClones:
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stack.close()

    def clone(self, template: PostgresDatabaseTemplate, *, drivername: str) -> str:
        """Create and register a writable clone, returning its URL with the requested driver."""
        url = self._stack.enter_context(template.clone(self._names()))
        return make_url(url).set(drivername=drivername).render_as_string(hide_password=False)


def _database_url(admin_url: str, db_name: str) -> str:
    return make_url(admin_url).set(database=db_name).render_as_string(hide_password=False)


def _require_safe_db_name(db_name: str) -> None:
    """Raise if db_name contains characters outside [a-zA-Z0-9_]."""
    if not _SAFE_DB_NAME.match(db_name):
        raise ValueError(f"Unsafe database name for test teardown: {db_name!r} (must match [a-zA-Z0-9_]+)")


def create_database_sync(
    admin_url: str, db_name: str, *, extensions: Sequence[str] = (), template: str | None = None
) -> str:
    """Create a database and its extensions, returning its URL.

    Extensions are installed here rather than by whatever runs migrations because the interesting
    ones are untrusted — pgvector among them — so only a superuser can install them, which in a
    deployment means the operator that owns the cluster rather than the application's own role.
    When ``template`` is set, the database starts as a physical copy of that database.
    """
    _require_safe_db_name(db_name)
    if template is not None:
        _require_safe_db_name(template)
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as conn:
            template_clause = f' TEMPLATE "{template}"' if template is not None else ""
            conn.execute(text(f'CREATE DATABASE "{db_name}"{template_clause}'))
    finally:
        admin_engine.dispose()

    db_url = _database_url(admin_url, db_name)
    try:
        if extensions:
            engine = create_engine(db_url, isolation_level="AUTOCOMMIT")
            try:
                with engine.connect() as conn:
                    for extension in extensions:
                        conn.execute(text(f'CREATE EXTENSION IF NOT EXISTS "{extension}"'))
            finally:
                engine.dispose()
    except BaseException:
        # The caller has not received the database yet, so its cleanup is not registered.
        force_drop_database_sync(admin_url, db_name)
        raise
    return db_url


async def create_database_async(admin_url: str, db_name: str, *, template: str | None = None) -> str:
    """Create a database and its URL, as an async SQLAlchemy URL.

    When ``template`` is set, the database starts as a physical copy of that database.
    """
    _require_safe_db_name(db_name)
    if template is not None:
        _require_safe_db_name(template)
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            template_clause = f' TEMPLATE "{template}"' if template is not None else ""
            await conn.execute(text(f'CREATE DATABASE "{db_name}"{template_clause}'))
    finally:
        await engine.dispose()
    return _database_url(admin_url, db_name)


async def force_drop_database(admin_url: str, db_name: str) -> None:
    """Terminate all connections to db_name and drop it (async).

    Creates a temporary AUTOCOMMIT engine from admin_url. Intended for test teardown.
    """
    _require_safe_db_name(db_name)
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(_TERMINATE_SQL, {"db_name": db_name})
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
    finally:
        await engine.dispose()


def force_drop_database_sync(admin_url: str, db_name: str) -> None:
    """Terminate all connections to db_name and drop it (sync).

    Creates a temporary AUTOCOMMIT engine from admin_url. Intended for test teardown.
    """
    _require_safe_db_name(db_name)
    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            conn.execute(_TERMINATE_SQL, {"db_name": db_name})
            conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
    finally:
        engine.dispose()


class PostgresDatabaseTemplate:
    """A migrated database that tests can clone into isolated writable databases.

    Create and migrate one template per session, then clone it once per test. A template can itself
    be based on another template, which lets an integration suite add a second service's schema
    once without repeating the first service's migrations.
    """

    def __init__(
        self, admin_url: str, template_name: str, *, template: str | None = None, extensions: Sequence[str] = ()
    ) -> None:
        self.admin_url = admin_url
        self.template_name = template_name
        self.source_template = template
        self.extensions = tuple(extensions)

    def __enter__(self) -> PostgresDatabaseTemplate:
        create_database_sync(
            self.admin_url, self.template_name, extensions=self.extensions, template=self.source_template
        )
        return self

    def __exit__(self, *_exc: object) -> None:
        force_drop_database_sync(self.admin_url, self.template_name)

    @property
    def url(self) -> str:
        """Template URL, retaining the driver's scheme and connection query parameters."""
        return _database_url(self.admin_url, self.template_name)

    @contextmanager
    def clone(self, db_name: str) -> Iterator[str]:
        """Yield an isolated writable copy, then drop it."""
        url = create_database_sync(self.admin_url, db_name, template=self.template_name)
        try:
            yield url
        finally:
            force_drop_database_sync(self.admin_url, db_name)


@contextmanager
def migrated_database_template(
    admin_url: str,
    template_name: str,
    runner: MigrationRunner,
    *,
    template: str | None = None,
    extensions: Sequence[str] = (),
) -> Iterator[PostgresDatabaseTemplate]:
    """Create a template, apply its migration history once, and clean up on exit."""
    with PostgresDatabaseTemplate(
        admin_url, template_name, template=template, extensions=extensions
    ) as migrated_template:
        runner.apply(migrated_template.url)
        yield migrated_template
