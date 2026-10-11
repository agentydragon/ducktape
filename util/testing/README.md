# Shared testing helpers

## PostgreSQL schema templates

For suites that need a migrated schema but isolated writable state per test, create and migrate one
template for the session, then clone it into a fresh database for each case. Import the shared
fixtures directly into the owning package conftest; keep this wiring local to the suites that need
Postgres so unrelated tests do not load Docker or migration dependencies.

```python
from util.testing.postgres import DatabaseClones, PostgresDatabaseTemplate, migrated_database_template
from util.testing.postgres_fixtures import database_clones, database_names, postgres_admin_url, postgres_container


@pytest.fixture(scope="session")
def migrated_template(postgres_admin_url: str) -> Iterator[PostgresDatabaseTemplate]:
    with migrated_database_template(
        postgres_admin_url,
        "service_test_template",
        SERVICE_RUNNER,
    ) as template:
        yield template


@pytest.fixture
def db_url(migrated_template: PostgresDatabaseTemplate, database_clones: DatabaseClones) -> str:
    return database_clones.clone(migrated_template, drivername="postgresql+asyncpg")
```

`database_names` allocates safe, deterministic, unique names from the current pytest node id. Use it
directly when a test needs a name but manages database retention or teardown itself. `database_clones`
owns an `ExitStack` for the test and drops every clone after dependent fixtures finish; specify each
clone's SQLAlchemy driver explicitly. The shared `postgres_admin_url` uses psycopg, and callers can
use `make_url` to preserve its credentials and query parameters while changing drivers.

`migrated_database_template` creates a template and applies its `MigrationRunner` once.
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
