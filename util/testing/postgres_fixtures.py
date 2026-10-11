"""Shared Postgres testcontainer helper + session fixture.

``postgres_container`` and ``postgres_image`` are the shared session fixtures — packages
pull both into their conftest with a bare import (no alias — pytest registers the fixture
by the dict key, so an alias would register under the wrong name; and no ``# noqa`` needed
in conftests, where F401 is ignored). Override ``postgres_image`` in a suite that needs a
non-default image.
``start_postgres_container`` is the underlying preload+start helper for packages that
wrap it (e.g. props, which adds a tracing span).

Both images are marked ``immutable`` upstream tags, so ``load_oci_image`` skips the
push on a warm worker by itself — this module no longer checks.

The shared ``postgres_admin_url`` uses the sync psycopg driver. Tests that need asyncpg
should change the driver explicitly on their per-test database URL. Suites that need a
different PostgreSQL image can override ``postgres_image`` in their conftest.
"""

import contextlib
import logging
import time
from collections.abc import Generator, Iterator

import pytest
from _pytest.fixtures import FixtureFunctionDefinition
from sqlalchemy.engine import URL
from testcontainers.postgres import PostgresContainer

from third_party.containers import postgres_18, ryuk
from util.db_migrations import MigrationRunner
from util.oci import OciImage, load_oci_image
from util.testing.postgres import (
    DatabaseClones,
    DatabaseNames,
    PostgresDatabaseTemplate,
    database_name_for_test,
    migrated_database_template,
)
from util.testing.undeclared_outputs import undeclared_outputs_dir

# SQLAlchemy loads the psycopg dialect from the URL scheme at runtime.
# gazelle:include_dep @pypi//psycopg

logger = logging.getLogger(__name__)


@contextlib.contextmanager
def _onto_disk() -> Iterator[None]:
    """Mirror this module's and `util.oci`'s progress into an undeclared output, as it happens.

    `logger.info` on its own does not survive the one case these lines exist for. pytest buffers
    captured output and a Bazel timeout kills the process, so the account of where the time went
    dies with the buffer — which is why the log of a wedged target is empty rather than damning. A
    file handler flushes per record, so whatever was written before the kill is still on disk and
    still uploaded.
    """
    handler = logging.FileHandler(undeclared_outputs_dir() / "container_setup.log")
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(message)s"))
    watched = [logging.getLogger("util.oci"), logger]
    levels = [each.level for each in watched]
    for each in watched:
        each.addHandler(handler)
        each.setLevel(logging.INFO)
    try:
        yield
    finally:
        for each, level in zip(watched, levels, strict=True):
            each.removeHandler(handler)
            each.setLevel(level)
        handler.close()


def start_postgres_container(image: OciImage = postgres_18.IMAGE) -> PostgresContainer:
    """Preload Ryuk + the Postgres image (skipping any the daemon already has), then start it.

    `image` selects a different Postgres build — e.g. `PGVECTOR_PG18` for packages whose schema
    needs the `vector` extension, which the stock image does not ship.

    **Every step says how long it took.** This runs in session fixture setup, before pytest has
    emitted a line, so when it wedges the whole test log is silence — which is how a five-minute
    hang came to be read as a slow test (<../../devinfra/debug/2026_08_14_docker_test_timeouts.md>). The
    load itself is serialised per tag inside `load_oci_image`; what is left unbounded is the
    daemon's own container start, so the least this can do is name where the time went.
    """
    with _onto_disk():
        for preloaded in (ryuk.IMAGE, image):
            load_oci_image(preloaded)
        container = PostgresContainer(image=image.tag, username="postgres", password="postgres", dbname="postgres")
        started = time.monotonic()
        logger.info("Starting %s", image.tag)
        container.start()
        logger.info("Postgres ready in %.1fs", time.monotonic() - started)
        return container


@pytest.fixture(scope="session")
def postgres_image() -> OciImage:
    """Default PostgreSQL image; suites can override this fixture for extensions or settings."""
    return postgres_18.IMAGE


@pytest.fixture(scope="session")
def postgres_container(postgres_image: OciImage) -> Generator[PostgresContainer]:
    container = start_postgres_container(postgres_image)
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture(scope="session")
def postgres_admin_url(postgres_container: PostgresContainer) -> str:
    """Build the sync psycopg admin URL for the shared Postgres container."""
    return URL.create(
        "postgresql+psycopg",
        username="postgres",
        password="postgres",
        host=postgres_container.get_container_host_ip(),
        port=int(postgres_container.get_exposed_port(5432)),
        database="postgres",
    ).render_as_string(hide_password=False)


@pytest.fixture
def database_names(request: pytest.FixtureRequest) -> DatabaseNames:
    """Allocate safe unique PostgreSQL names scoped to the current pytest node."""
    return DatabaseNames(request.node.nodeid)


@pytest.fixture
def database_clones(database_names: DatabaseNames) -> Iterator[DatabaseClones]:
    """Own template clones for one test and drop them after dependent fixtures finish."""
    with DatabaseClones(database_names) as clones:
        yield clones


def migrated_database_fixtures(
    runner: MigrationRunner, *, drivername: str, template_name: str = "database_template", url_name: str = "db_url"
) -> tuple[FixtureFunctionDefinition, FixtureFunctionDefinition]:
    """Declare a session migration template and a function-scoped clone URL fixture.

    Bind both returned fixtures in the suite's conftest. Explicit names let suites compose
    several databases without the URL fixture resolving a different service's template.
    """

    @pytest.fixture(scope="session", name=template_name)
    def template(postgres_admin_url: str) -> Iterator[PostgresDatabaseTemplate]:
        with migrated_database_template(postgres_admin_url, database_name_for_test(template_name), runner) as migrated:
            yield migrated

    @pytest.fixture(name=url_name)
    def url(request: pytest.FixtureRequest, database_clones: DatabaseClones) -> str:
        source = request.getfixturevalue(template_name)
        return database_clones.clone(source, drivername=drivername)

    return template, url
