"""Shared helper for the standard Postgres testcontainer.

``start_postgres_container`` preloads Ryuk + Postgres 18 into the Docker daemon
and starts the standard container (user/pass/db ``postgres``). Each package
defines its own session-scoped ``postgres_container`` fixture that calls this,
so the expensive preload + start logic is shared while the fixture itself stays
local — keeping the preload scoped to test sessions that actually start a
container. Postgres-free test targets never request the fixture, so they skip
the ~15s image load entirely (previously an ``autouse`` session fixture taxed them).

Packages keep their own ``postgres_admin_url(postgres_container)`` because the
SQLAlchemy driver differs (``postgresql+psycopg://`` vs ``postgresql+asyncpg://``).
"""

from testcontainers.postgres import PostgresContainer

from third_party.containers.rlocations import POSTGRES_18, RYUK
from util.oci import load_oci_image


def start_postgres_container() -> PostgresContainer:
    """Preload Ryuk + Postgres 18, then start the standard container."""
    load_oci_image(RYUK)
    load_oci_image(POSTGRES_18)
    container = PostgresContainer(image=POSTGRES_18.tag, username="postgres", password="postgres", dbname="postgres")
    container.start()
    return container
