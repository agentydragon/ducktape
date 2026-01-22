"""FastAPI application for props backend - unified dashboard, proxy, and eval APIs.

This is the unified props backend that includes:
- Dashboard API: /api/stats, /api/runs, /api/gt
- LLM Proxy: /v1/responses
- Registry Proxy: /v2/*
- Eval API: /api/eval/run_critic, /api/eval/wait_until_graded
"""

from __future__ import annotations

import logging
import os
import sys
import traceback
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import aiodocker
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from props.backend.auth import AuthMiddleware
from props.backend.routes import eval, ground_truth, llm, registry, runs, stats
from props.cli.common_options import DEFAULT_LLM_PROXY_URL
from props.cli.resources import get_database_config
from props.core.agent_registry import AgentRegistry
from props.grader.daemon_manager import DaemonManager

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

# --- Logging Configuration ---


def configure_logging() -> None:
    """Configure structured logging for the backend."""
    log_level = os.environ.get("PROPS_LOG_LEVEL", "INFO").upper()
    log_file = os.environ.get("PROPS_LOG_FILE")

    # Create formatter
    formatter = logging.Formatter(fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    # Root logger
    root = logging.getLogger()
    root.setLevel(log_level)

    # Console handler (always)
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    root.addHandler(console)

    # File handler (if configured)
    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    # Quiet noisy loggers
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiodocker").setLevel(logging.WARNING)


# Configure logging on module import
configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan handler."""
    logger.info("Starting props backend...")

    # Create resources
    docker_client = aiodocker.Docker()
    db_config = get_database_config()

    llm_proxy_url = getattr(app.state, "llm_proxy_url", None) or DEFAULT_LLM_PROXY_URL

    # Registry owns resources and orchestrates agent runs
    app.state.registry = AgentRegistry(docker_client=docker_client, db_config=db_config, llm_proxy_url=llm_proxy_url)

    # Start grader daemons (required)
    grader_model = os.environ.get("PROPS_GRADER_MODEL")
    if not grader_model:
        raise RuntimeError("PROPS_GRADER_MODEL environment variable is required")
    daemon_manager = DaemonManager(registry=app.state.registry, model=grader_model)
    await daemon_manager.start_all()
    app.state.daemon_manager = daemon_manager
    logger.info(f"Grader daemons started (model: {grader_model})")

    logger.info("Props backend ready")
    yield

    # Cleanup
    logger.info("Shutting down props backend...")
    await daemon_manager.shutdown()
    await app.state.registry.close()
    logger.info("Props backend stopped")


def create_app(*, static_dir: Path | None = None) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        static_dir: Optional path to static files directory for frontend assets.
    """
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
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
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
