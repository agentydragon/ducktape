# Shared testing helpers

## PostgreSQL schema templates

For suites that need a migrated schema but isolated writable state per test, create and migrate one
template for the session, then clone it into a fresh database for each case:

```python
from util.testing.postgres import PostgresDatabaseTemplate, database_name_for_test


@pytest.fixture(scope="session")
def migrated_template(postgres_admin_url: str) -> Iterator[PostgresDatabaseTemplate]:
    with PostgresDatabaseTemplate(
        postgres_admin_url,
        "service_test_template",
        extensions=("vector",),  # optional; installed before the caller applies migrations
    ) as template:
        SERVICE_RUNNER.apply(template.url)
        yield template


@pytest.fixture
def db_url(migrated_template: PostgresDatabaseTemplate, request: pytest.FixtureRequest) -> Iterator[str]:
    name = database_name_for_test(request.node.nodeid)
    with migrated_template.clone(name) as url:
        yield url
```

The service or test package owns its migration runner and applies it to `template.url`; the shared
helper only creates, clones, and drops databases. Its URL retains the sync driver's scheme and query
parameters from `postgres_admin_url`; convert it explicitly with SQLAlchemy `make_url` when a test
needs an async driver.

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
