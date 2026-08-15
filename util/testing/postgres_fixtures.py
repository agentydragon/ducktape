"""Shared Postgres testcontainer helper + session fixture.

``postgres_container`` is the shared session fixture — packages pull it into their
conftest with a bare ``from util.testing.postgres_fixtures import postgres_container``
(no alias — pytest registers the fixture by the dict key, so an alias would register
under the wrong name; and no ``# noqa`` needed in conftests, where F401 is ignored).
``start_postgres_container`` is the underlying preload+start helper for packages that
wrap it (e.g. props, which adds a tracing span).

Both images are marked ``immutable`` upstream tags, so ``load_oci_image`` skips the
push on a warm worker by itself — this module no longer checks.

Packages keep their own ``postgres_admin_url(postgres_container)`` because the
SQLAlchemy driver differs (``postgresql+psycopg://`` vs ``postgresql+asyncpg://``).
"""

import contextlib
import logging
import time
from collections.abc import Generator, Iterator

import pytest
from testcontainers.postgres import PostgresContainer

from third_party.containers.rlocations import POSTGRES_18, RYUK
from util.oci import OciImage, load_oci_image
from util.testing.undeclared_outputs import undeclared_outputs_dir

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


def start_postgres_container(image: OciImage = POSTGRES_18) -> PostgresContainer:
    """Preload Ryuk + the Postgres image (skipping any the daemon already has), then start it.

    `image` selects a different Postgres build — e.g. `PGVECTOR_PG18` for packages whose schema
    needs the `vector` extension, which the stock image does not ship.

    **Every step says how long it took.** This runs in session fixture setup, before pytest has
    emitted a line, so when it wedges the whole test log is silence — which is how a five-minute
    hang came to be read as a slow test (<../../debug/2026_08_14_docker_test_timeouts.md>). The
    load itself is serialised per tag inside `load_oci_image`; what is left unbounded is the
    daemon's own container start, so the least this can do is name where the time went.
    """
    with _onto_disk():
        for preloaded in (RYUK, image):
            load_oci_image(preloaded)
        container = PostgresContainer(image=image.tag, username="postgres", password="postgres", dbname="postgres")
        started = time.monotonic()
        logger.info("Starting %s", image.tag)
        container.start()
        logger.info("Postgres ready in %.1fs", time.monotonic() - started)
        return container


@pytest.fixture(scope="session")
def postgres_container() -> Generator[PostgresContainer]:
    container = start_postgres_container()
    try:
        yield container
    finally:
        container.stop()
