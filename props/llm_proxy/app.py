"""Standalone LLM proxy app — the agent **data plane**, split out of the unified
backend so the dashboard/API can roll without disrupting in-flight agents.

Serves ``/v1/responses`` and ``/v1/chat/completions`` (auth + budget + cost + upstream routing). It has
no frontend, orchestration, registry proxy, or SSO. The router's only app-state requirements
are ``admin_db`` (async PostgreSQL sessions for auth + budget/cost/upstream
queries) and ``config`` (``LLMProxyConfig``, for upstream routing).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from props.config import LLMProxyConfig, load_proxy_config_from_env
from props.data_plane import add_probe_routes, admin_database
from props.db.async_database import AsyncDatabase
from props.llm_proxy import routes

logger = logging.getLogger(__name__)


def create_app(*, db: AsyncDatabase | None = None, config: LLMProxyConfig | None = None) -> FastAPI:
    """Build the LLM proxy app.

    `db`/`config` are injected in tests; in production they are loaded from the
    environment (``PG*`` for the DB, ``PROPS_CONFIG_FILE`` for the config).
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with admin_database(db) as admin_db:
            app.state.admin_db = admin_db
            app.state.config = config or load_proxy_config_from_env()
            logger.info("LLM proxy ready")
            yield

    app = FastAPI(title="props-llm-proxy", lifespan=lifespan)
    app.include_router(routes.router)
    add_probe_routes(app)
    return app
