"""Shared Postgres testcontainer helper + session fixture.

``postgres_container`` is the shared session fixture — packages pull it into their
conftest with a bare ``from util.testing.postgres_fixtures import postgres_container``
(no alias — pytest registers the fixture by the dict key, so an alias would register
under the wrong name; and no ``# noqa`` needed in conftests, where F401 is ignored).
``start_postgres_container`` is the underlying preload+start helper for packages that
wrap it (e.g. props, which adds a tracing span).

The preload is skipped when the daemon already has both images: ``postgres:18`` and
``testcontainers/ryuk:0.8.1`` are immutable upstream tags, so on a warm worker reused
across test targets this avoids re-pushing ~30s of layers for nothing.

Packages keep their own ``postgres_admin_url(postgres_container)`` because the
SQLAlchemy driver differs (``postgresql+psycopg://`` vs ``postgresql+asyncpg://``).
"""

import subprocess
from collections.abc import Generator

import pytest
from testcontainers.postgres import PostgresContainer

from third_party.containers.rlocations import POSTGRES_18, RYUK
from util.oci import load_oci_image


def _daemon_has_image(tag: str) -> bool:
    """Whether the local Docker daemon already has ``tag`` (so the load can be skipped)."""
    return subprocess.run(["docker", "image", "inspect", tag], capture_output=True, check=False).returncode == 0


def start_postgres_container() -> PostgresContainer:
    """Preload Ryuk + Postgres 18 (unless the daemon already has them), then start the container."""
    if not (_daemon_has_image(RYUK.tag) and _daemon_has_image(POSTGRES_18.tag)):
        load_oci_image(RYUK)
        load_oci_image(POSTGRES_18)
    container = PostgresContainer(image=POSTGRES_18.tag, username="postgres", password="postgres", dbname="postgres")
    container.start()
    return container


@pytest.fixture(scope="session")
def postgres_container() -> Generator[PostgresContainer]:
    container = start_postgres_container()
    try:
        yield container
    finally:
        container.stop()
