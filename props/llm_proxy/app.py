"""Standalone LLM proxy app — the agent **data plane**, split out of the unified
backend so the dashboard/API can roll without disrupting in-flight agents.

Serves ``/v1/responses`` and ``/v1/chat/completions`` (auth + budget + cost + upstream routing). It has
no frontend, orchestration, registry proxy, or SSO. The router's only app-state requirements
are ``admin_db`` (async PostgreSQL sessions for auth + budget/cost/upstream
queries) and ``config`` (``LLMProxyConfig``, for upstream routing).
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

from props.config import LLMProxyConfig, load_proxy_config_from_env
from props.db.async_database import AsyncDatabase
from props.db.config import DatabaseConfig
from props.llm_proxy import routes
from util.logging import LogLevel, configure_logging

configure_logging(
    log_output=os.environ.get("PROPS_LOG_OUTPUT", "stderr"), log_level=os.environ.get("PROPS_LOG_LEVEL", LogLevel.INFO)
)
logger = logging.getLogger(__name__)


def create_app(*, db: AsyncDatabase | None = None, config: LLMProxyConfig | None = None) -> FastAPI:
    """Build the LLM proxy app.

    `db`/`config` are injected in tests; in production they are loaded from the
    environment (``PG*`` for the DB, ``PROPS_CONFIG_FILE`` for the config).
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            if db is None:
                admin_db = await stack.enter_async_context(AsyncDatabase(DatabaseConfig()))
                await admin_db.verify()
            else:
                admin_db = db
            app.state.admin_db = admin_db
            app.state.config = config or load_proxy_config_from_env()
            logger.info("LLM proxy ready")
            yield

    app = FastAPI(title="props-llm-proxy", lifespan=lifespan)
    app.include_router(routes.router)

    # The proxy has no slow startup (just DB + config wiring), so readiness is
    # equivalent to liveness.
    @app.get("/health", response_class=PlainTextResponse)
    async def health() -> str:
        return "ok"

    @app.get("/readyz", response_class=PlainTextResponse)
    async def readyz() -> str:
        return "ok"

    return app
