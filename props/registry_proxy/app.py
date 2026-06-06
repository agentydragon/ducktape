"""Standalone OCI registry proxy for props agent images.

Serves only the OCI Distribution API routes under ``/v2/*``. This app does not
include frontend, orchestration, SSO, LLM proxy, or specimen-sync startup work.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

from props.db.config import DatabaseConfig
from props.db.database import Database
from props.registry_proxy import routes
from util.logging import LogLevel, configure_logging

configure_logging(
    log_output=os.environ.get("PROPS_LOG_OUTPUT", "stderr"), log_level=os.environ.get("PROPS_LOG_LEVEL", LogLevel.INFO)
)
logger = logging.getLogger(__name__)


def create_app(*, db: Database | None = None) -> FastAPI:
    """Build the registry proxy app.

    ``db`` is injected in tests; production loads it from the standard ``PG*``
    environment. Upstream registry config is read by the router from
    ``PROPS_REGISTRY_UPSTREAM_*`` environment variables.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.admin_db = db or Database(DatabaseConfig())
        logger.info("Registry proxy ready")
        yield

    app = FastAPI(title="props-registry-proxy", lifespan=lifespan)
    app.include_router(routes.router)

    @app.get("/health", response_class=PlainTextResponse)
    async def health() -> str:
        return "ok"

    @app.get("/readyz", response_class=PlainTextResponse)
    async def readyz() -> str:
        return "ok"

    return app
