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

import logging
import time
from collections.abc import Generator

import pytest
from testcontainers.postgres import PostgresContainer

from third_party.containers.rlocations import POSTGRES_18, RYUK
from util.oci import load_oci_image

logger = logging.getLogger(__name__)


def start_postgres_container() -> PostgresContainer:
    """Preload Ryuk + Postgres 18 (skipping any the daemon already has), then start the container.

    **Every step says how long it took.** This runs in session fixture setup, before pytest has
    emitted a line, so when it wedges the whole test log is silence — which is how a five-minute
    hang came to be read as a slow test (<../../debug/2026_08_14_docker_test_timeouts.md>). The
    load itself is serialised per tag inside `load_oci_image`; what is left unbounded is the
    daemon's own container start, so the least this can do is name where the time went.
    """
    for image in (RYUK, POSTGRES_18):
        load_oci_image(image)
    container = PostgresContainer(image=POSTGRES_18.tag, username="postgres", password="postgres", dbname="postgres")
    started = time.monotonic()
    logger.info("Starting %s", POSTGRES_18.tag)
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
