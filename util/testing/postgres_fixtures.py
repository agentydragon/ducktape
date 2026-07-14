"""Shared helper for the standard Postgres testcontainer.

``start_postgres_container`` preloads Ryuk + Postgres 18 into the Docker daemon
(if it doesn't already have them) and starts the standard container
(user/pass/db ``postgres``). Each package defines its own session-scoped
``postgres_container`` fixture that calls this, so the expensive preload + start
logic is shared while the fixture itself stays local — keeping the preload scoped
to test sessions that actually start a container. Postgres-free test targets
never request the fixture, so they skip the load entirely.

The preload is skipped when the daemon already has both images: ``postgres:18``
and ``testcontainers/ryuk:0.8.1`` are immutable upstream tags, so on a warm worker
(e.g. an RBE runner reused across test targets in one invocation) this avoids
re-pushing ~30s of layers for nothing.

Packages keep their own ``postgres_admin_url(postgres_container)`` because the
SQLAlchemy driver differs (``postgresql+psycopg://`` vs ``postgresql+asyncpg://``).
"""

import subprocess

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
