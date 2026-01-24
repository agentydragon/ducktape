"""FastAPI application for props backend - unified dashboard, proxy, and eval APIs.

This is the unified props backend that includes:
- Dashboard API: /api/stats, /api/runs, /api/gt
- LLM Proxy: /v1/responses
- Registry Proxy: /v2/*
- Eval API: /api/eval/run_critic, /api/eval/grading_status/{critic_run_id}

Note: wait_until_graded is implemented inside containers by polling the grading_pending
view directly, not as a REST endpoint. The grading_status endpoint provides a non-blocking
status check that containers can poll.
"""

from __future__ import annotations

import logging
import os
import traceback
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import aiodocker
import asyncpg
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from cli_util.logging import LogLevel, configure_logging
from props.backend.auth import AuthMiddleware
from props.backend.routes import eval, ground_truth, llm, registry, runs, stats
from props.cli.resources import get_database_config
from props.orchestration.agent_registry import AgentRegistry
from props.orchestration.grader_supervisor import GraderSupervisor

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

# Configure logging on module import
configure_logging(
    log_output=os.environ.get("PROPS_LOG_OUTPUT", "stderr"), log_level=os.environ.get("PROPS_LOG_LEVEL", LogLevel.INFO)
)
logger = logging.getLogger(__name__)

# Environment variable names for configuration
ENV_LLM_PROXY_URL = "PROPS_LLM_PROXY_URL"
ENV_GRADER_MODEL = "PROPS_GRADER_MODEL"
ENV_CORS_ORIGINS = "PROPS_CORS_ORIGINS"

# Default CORS origins for development
DEFAULT_CORS_ORIGINS = "http://localhost:5173,http://127.0.0.1:5173"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info("Starting props backend...")

    # Create resources
    docker_client = aiodocker.Docker()
    db_config = get_database_config()

    # LLM proxy URL is required - check app.state first (for tests), then env var
    llm_proxy_url = getattr(app.state, "llm_proxy_url", None) or os.environ.get(ENV_LLM_PROXY_URL)
    if not llm_proxy_url:
        raise RuntimeError(f"LLM proxy URL required: set {ENV_LLM_PROXY_URL} or app.state.llm_proxy_url")

    # Registry owns resources and orchestrates agent runs
    app.state.registry = AgentRegistry(docker_client=docker_client, db_config=db_config, llm_proxy_url=llm_proxy_url)

    # Initialize daemon manager if configured
    # Daemon manager listens for pg_notify on snapshot_created channel and spawns daemons automatically
    grader_model = getattr(app.state, "grader_model", None) or os.environ.get(ENV_GRADER_MODEL)
    if grader_model:
        # Connection factory for pg_notify listener
        async def connect() -> asyncpg.Connection[asyncpg.Record]:
            return await asyncpg.connect(db_config.admin.url())

        app.state.grader_supervisor = GraderSupervisor(registry=app.state.registry, connect=connect, model=grader_model)
        await app.state.grader_supervisor.start()
        logger.info(f"Daemon manager started (model: {grader_model})")
    else:
        app.state.grader_supervisor = None
        logger.info(f"Daemon manager disabled ({ENV_GRADER_MODEL} not set)")

    logger.info("Props backend ready")
    yield

    # Cleanup
    logger.info("Shutting down props backend...")
    if app.state.grader_supervisor:
        await app.state.grader_supervisor.shutdown()
    await app.state.registry.close()
    logger.info("Props backend stopped")


def create_app(*, static_dir: Path | None = None) -> FastAPI:
    app = FastAPI(
        title="Props Backend",
        description="Unified props backend: dashboard, proxies (LLM/registry), and eval APIs",
        version="0.1.0",
        lifespan=lifespan,
        debug=True,
    )

    # Auth middleware - parses credentials and attaches to request.state
    app.add_middleware(AuthMiddleware)

    # CORS for development (Vite dev server on different port)
    cors_origins = os.environ.get(ENV_CORS_ORIGINS, DEFAULT_CORS_ORIGINS)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[origin.strip() for origin in cors_origins.split(",")],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Dashboard API routes
    app.include_router(stats.router, prefix="/api/stats", tags=["stats"])
    app.include_router(runs.router, prefix="/api/runs", tags=["runs"])
    app.include_router(ground_truth.router, prefix="/api/gt", tags=["ground_truth"])

    # Eval API routes (for PO/PI agents)
    app.include_router(eval.router, prefix="/api/eval", tags=["eval"])

    # LLM Proxy routes (for agents)
    app.include_router(llm.router, tags=["llm_proxy"])

    # Registry Proxy routes (for agents and admin)
    app.include_router(registry.router, tags=["registry_proxy"])

    # Health check
    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # Dev mode: show full tracebacks in responses
    @app.exception_handler(Exception)
    async def debug_exception_handler(request: Request, exc: Exception) -> PlainTextResponse:
        return PlainTextResponse(
            content="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)), status_code=500
        )

    # Mount static files if directory provided (must be last - catches all remaining paths)
    if static_dir and static_dir.exists():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")

    return app


# Default app instance for uvicorn
app = create_app()
