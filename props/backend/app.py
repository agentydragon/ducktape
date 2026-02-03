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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import aiodocker
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from cli_util.logging import LogLevel, configure_logging
from props.backend.auth import AuthMiddleware
from props.backend.routes import agent_definitions, eval, ground_truth, llm, registry, runs, stats
from props.config import PropsConfig, load_config_from_env
from props.core.oci_utils import RegistryProxyConfig, get_registry_proxy_config
from props.db.config import DatabaseConfig
from props.db.database import Database
from props.orchestration.agent_registry import AgentRegistry
from props.orchestration.grader_supervisor import GraderSupervisor

# Configure logging on module import
configure_logging(
    log_output=os.environ.get("PROPS_LOG_OUTPUT", "stderr"), log_level=os.environ.get("PROPS_LOG_LEVEL", LogLevel.INFO)
)
logger = logging.getLogger(__name__)

ENV_GRADER_MODEL = "PROPS_GRADER_MODEL"
ENV_CORS_ORIGINS = "PROPS_CORS_ORIGINS"

DEFAULT_CORS_ORIGINS = "http://localhost:5173,http://127.0.0.1:5173"


@dataclass(frozen=True)
class BackendDeps:
    """Explicit dependencies for the backend lifespan, replacing getattr on app.state."""

    config: PropsConfig
    registry_proxy_config: RegistryProxyConfig
    grader_model: str | None = None


def _make_lifespan(deps: BackendDeps):
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info("Starting props backend...")

        docker_client = aiodocker.Docker()
        db_config = DatabaseConfig()
        db = Database(db_config)
        app.state.admin_db = db

        app.state.registry = AgentRegistry(
            docker_client=docker_client,
            db=db,
            db_config=db_config,
            agent_base_env=deps.config.agent_env,
            registry_config=deps.registry_proxy_config,
        )

        if deps.grader_model:
            app.state.grader_supervisor = GraderSupervisor(
                registry=app.state.registry, db_config=db_config, model=deps.grader_model, db=db
            )
            await app.state.grader_supervisor.start()
            logger.info(f"Daemon manager started (model: {deps.grader_model})")
        else:
            app.state.grader_supervisor = None
            logger.info(f"Daemon manager disabled ({ENV_GRADER_MODEL} not set)")

        logger.info("Props backend ready")
        yield

        logger.info("Shutting down props backend...")
        if app.state.grader_supervisor:
            await app.state.grader_supervisor.shutdown()
        await app.state.registry.close()
        db.dispose()
        logger.info("Props backend stopped")

    return lifespan


def create_app(*, deps: BackendDeps, static_dir: Path | None = None) -> FastAPI:
    app = FastAPI(
        title="Props Backend",
        description="Unified props backend: dashboard, proxies (LLM/registry), and eval APIs",
        version="0.1.0",
        lifespan=_make_lifespan(deps),
        debug=True,
    )

    app.add_middleware(AuthMiddleware)

    cors_origins = os.environ.get(ENV_CORS_ORIGINS, DEFAULT_CORS_ORIGINS)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[origin.strip() for origin in cors_origins.split(",")],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(stats.router, prefix="/api/stats", tags=["stats"])
    app.include_router(runs.router, prefix="/api/runs", tags=["runs"])
    app.include_router(ground_truth.router, prefix="/api/gt", tags=["ground_truth"])
    app.include_router(agent_definitions.router, prefix="/api/definitions", tags=["definitions"])
    app.include_router(eval.router, prefix="/api/eval", tags=["eval"])
    app.include_router(llm.router, tags=["llm_proxy"])
    app.include_router(registry.router, tags=["registry_proxy"])

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.exception_handler(Exception)
    async def debug_exception_handler(request: Request, exc: Exception) -> PlainTextResponse:
        return PlainTextResponse(
            content="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)), status_code=500
        )

    if static_dir and static_dir.exists():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")

    return app


def default_deps() -> BackendDeps:
    return BackendDeps(
        config=load_config_from_env(),
        registry_proxy_config=get_registry_proxy_config(),
        grader_model=os.environ.get(ENV_GRADER_MODEL),
    )
