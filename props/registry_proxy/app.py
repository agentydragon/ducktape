"""Standalone OCI registry proxy for props agent images.

Serves only the OCI Distribution API routes under ``/v2/*``. This app does not
include frontend, orchestration, SSO, LLM proxy, or specimen-sync startup work.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from props.data_plane import add_probe_routes, admin_database
from props.db.async_database import AsyncDatabase
from props.registry_proxy import routes

logger = logging.getLogger(__name__)


def create_app(*, db: AsyncDatabase | None = None) -> FastAPI:
    """Build the registry proxy app.

    ``db`` is injected in tests; production loads it from the standard ``PG*``
    environment. Upstream registry config is read by the router from
    ``PROPS_REGISTRY_UPSTREAM_*`` environment variables.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with admin_database(db) as admin_db:
            app.state.admin_db = admin_db
            logger.info("Registry proxy ready")
            yield

    app = FastAPI(title="props-registry-proxy", lifespan=lifespan)
    app.include_router(routes.router)
    add_probe_routes(app)
    return app
