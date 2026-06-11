"""FastAPI application for props dashboard."""

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
from openai import AsyncOpenAI

from openai_utils.model import BoundOpenAIModel
from props.backend.routes import ground_truth, runs, stats
from props.core.agent_registry import AgentRegistry
from props.core.agent_workspace import WorkspaceManager
from props.core.cli.resources import get_database_config
from props.core.grader.daemon_manager import DaemonManager

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


def _create_grader_client() -> BoundOpenAIModel | None:
    """Create model client for grader daemons if configured.

    Returns None if PROPS_GRADER_MODEL is not set.
    """
    model = os.environ.get("PROPS_GRADER_MODEL")
    if not model:
        return None

    return BoundOpenAIModel(client=AsyncOpenAI(), model=model)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan handler."""
    logger.info("Starting props backend...")

    # Create resources
    docker_client = aiodocker.Docker()
    db_config = get_database_config()
    workspace_manager = WorkspaceManager.from_env()

    # Registry owns resources and orchestrates agent runs
    app.state.registry = AgentRegistry(
        docker_client=docker_client, db_config=db_config, workspace_manager=workspace_manager
    )

    # Optionally start grader daemons if model configured
    daemon_manager = None
    grader_client = _create_grader_client()
    if grader_client:
        daemon_manager = DaemonManager(registry=app.state.registry, client=grader_client)
        await daemon_manager.start_all()
        app.state.daemon_manager = daemon_manager
        logger.info(f"Grader daemons started (model: {grader_client.model})")
    else:
        logger.info("Grader daemons disabled (set PROPS_GRADER_MODEL to enable)")

    logger.info("Props backend ready")
    yield

    # Cleanup
    logger.info("Shutting down props backend...")
    if daemon_manager:
        await daemon_manager.shutdown()
    await app.state.registry.close()
    logger.info("Props backend stopped")


def create_app(*, static_dir: Path | None = None) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        static_dir: Optional path to static files directory for frontend assets.
    """
    app = FastAPI(
        title="Props Dashboard",
        description="Training and evaluation metrics dashboard",
        version="0.1.0",
        lifespan=lifespan,
        debug=True,
    )

    # CORS for development (Vite dev server on different port)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # API routes
    app.include_router(stats.router, prefix="/api/stats", tags=["stats"])
    app.include_router(runs.router, prefix="/api/runs", tags=["runs"])
    app.include_router(ground_truth.router, prefix="/api/gt", tags=["ground_truth"])

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

    # Mount static files if directory provided
    if static_dir and static_dir.exists():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")

    return app


# Default app instance for uvicorn
app = create_app()
