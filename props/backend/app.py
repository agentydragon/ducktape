"""FastAPI application for props backend - unified dashboard, proxy, and run APIs.

This is the props backend that includes:
- Dashboard API: /api/stats, /api/runs, /api/gt
- Registry Proxy: /v2/*
- Critic Run API: /api/runs/critic

The LLM proxy (/v1/responses) is now a separate service — see props/llm_proxy.

Note: wait_until_graded is implemented inside containers by polling the grading_pending
view directly, not as a REST endpoint.
"""

from __future__ import annotations

import asyncio
import logging
import os
import traceback
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from props.backend.oidc import build_oauth, load_oidc_settings
from props.backend.routes import agent_definitions, auth_routes, ground_truth, model_metadata, registry, runs, stats
from props.config import PropsConfig, load_config_from_env
from props.core.oci_utils import RegistryProxyConfig, get_registry_proxy_config
from props.db.config import DatabaseConfig
from props.db.database import Database
from props.db.setup import ensure_evaluator_role, upgrade_database
from props.db.sync.model_metadata import sync_model_metadata_with_session
from props.db.sync.sync import SpecimenBundle, refresh_examples_matview, sync_specimen
from props.orchestration.agent_registry import AgentRegistry
from props.orchestration.executor_factory import create_executor
from props.orchestration.grader_supervisor import GraderSupervisor
from util.logging import LogLevel, configure_logging

# Configure logging on module import
configure_logging(
    log_output=os.environ.get("PROPS_LOG_OUTPUT", "stderr"), log_level=os.environ.get("PROPS_LOG_LEVEL", LogLevel.INFO)
)
logger = logging.getLogger(__name__)

ENV_CORS_ORIGINS = "PROPS_CORS_ORIGINS"

DEFAULT_CORS_ORIGINS = "http://localhost:5173,http://127.0.0.1:5173"


@dataclass(frozen=True)
class BackendDeps:
    """Explicit dependencies for the backend lifespan, replacing getattr on app.state."""

    config: PropsConfig
    registry_proxy_config: RegistryProxyConfig
    backend_url: str
    grader_model: str | None = None
    host: str = "127.0.0.1"
    port: int = 8000


SPECIMENS_DIR = Path("/specimens")


def _sync_all_specimens(db: Database) -> None:
    """Scan /specimens/ directory and sync each specimen to the database."""
    if not SPECIMENS_DIR.exists():
        logger.warning(f"Specimens directory {SPECIMENS_DIR} not found, skipping sync")
        return

    synced = 0
    for data_yaml in sorted(SPECIMENS_DIR.rglob("specimen_data.yaml")):
        code_tar = data_yaml.parent / "specimen_code.tar"
        if not code_tar.exists():
            logger.warning(f"Missing code tar for {data_yaml}, skipping")
            continue
        bundle = SpecimenBundle.from_paths(code_tar, data_yaml)
        with db.session() as session:
            sync_specimen(session, bundle)
            session.commit()
        synced += 1

    logger.info(f"Synced {synced} specimens from {SPECIMENS_DIR}")

    # Refresh the materialized view (reads committed specimen data)
    with db.session() as session:
        refresh_examples_matview(session)


def _make_lifespan(deps: BackendDeps):
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Fast path: bind the HTTP server immediately so kubelet probes can reach
        # /health and /readyz. All the slow startup work (alembic migrations,
        # specimen sync, executor + registry construction, grader supervisor)
        # runs in a background task. Readiness is gated on `startup_complete`
        # so the Service won't route traffic until the slow work has finished.
        logger.info("Starting props backend (fast lifespan; slow init backgrounded)...")
        app.state.startup_complete = False
        app.state.grader_supervisor = None
        app.state.registry = None
        app.state.admin_db = None

        db_config = DatabaseConfig()
        db = Database(db_config)
        app.state.admin_db = db
        app.state.config = deps.config

        async def _slow_startup() -> None:
            if deps.config.auto_migrate:
                await asyncio.to_thread(upgrade_database, db.engine)
            await asyncio.to_thread(ensure_evaluator_role, db_config)

            def _sync_metadata() -> None:
                with db.session() as session:
                    stats = sync_model_metadata_with_session(session, deps.config)
                    if stats.added or stats.deleted:
                        logger.info(f"Model metadata synced: +{stats.added} added, -{stats.deleted} deleted")

            await asyncio.to_thread(_sync_metadata)

            if deps.config.auto_sync_specimens:
                await asyncio.to_thread(_sync_all_specimens, db)

            executor = await create_executor(deps.config.executor, db_config, deps.registry_proxy_config)
            logger.info("Using %s executor", deps.config.executor.type)
            model_parallelism_limits = {
                m.name: m.max_parallel_agents for m in deps.config.models if m.max_parallel_agents is not None
            }
            app.state.registry = AgentRegistry(
                executor=executor,
                db=db,
                db_config=db_config,
                backend_url=deps.backend_url,
                agent_base_env=deps.config.agent_env,
                registry_config=deps.registry_proxy_config,
                model_parallelism_limits=model_parallelism_limits,
                llm_base_url=deps.config.llm_proxy_url,
            )

            if deps.grader_model:
                app.state.grader_supervisor = GraderSupervisor(
                    registry=app.state.registry, db_config=db_config, model=deps.grader_model, db=db
                )
                await app.state.grader_supervisor.start()
                logger.info(f"Grader supervisor started (model: {deps.grader_model})")
            else:
                logger.info("Grader supervisor disabled (grader_model not set in config)")

            admin_token = db_config.basic_auth_token
            protocol = "https" if deps.port == 443 else "http"
            logger.info(f"Admin token: {admin_token}")
            logger.info(f"Admin URL: {protocol}://{deps.host}:{deps.port}/?token={admin_token}")
            logger.info("Props backend ready")
            app.state.startup_complete = True

        startup_task = asyncio.create_task(_slow_startup())

        # If the slow-startup task raises, asyncio will only log the unhandled
        # exception by default — the server keeps running NotReady forever.
        # That's worse than crashing: a stuck-NotReady pod silently fails any
        # rollout and obscures the underlying bug. Re-raise via os._exit so
        # kubelet restarts us and the failure is loud (matches the pre-change
        # behaviour where lifespan exceptions crashed the worker).
        def _on_startup_done(task: asyncio.Task[None]) -> None:
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                logger.exception("Background startup failed; exiting to trigger restart", exc_info=exc)
                os._exit(1)

        startup_task.add_done_callback(_on_startup_done)

        yield

        logger.info("Shutting down props backend...")
        if not startup_task.done():
            startup_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await startup_task
        if app.state.grader_supervisor is not None:
            await app.state.grader_supervisor.shutdown()
        if app.state.registry is not None:
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

    cors_origins = os.environ.get(ENV_CORS_ORIGINS, DEFAULT_CORS_ORIGINS)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[origin.strip() for origin in cors_origins.split(",")],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Optional Authentik SSO for browser users. When unconfigured (local dev,
    # tests, docker-compose) the dashboard stays token-only; machine clients
    # always use the Postgres-credential Bearer/Basic path in auth.py.
    oidc_settings = load_oidc_settings()
    app.state.oidc_settings = oidc_settings
    app.state.oauth = None
    if oidc_settings is not None:
        app.add_middleware(
            SessionMiddleware,
            secret_key=oidc_settings.session_secret,
            session_cookie="props_session",
            https_only=oidc_settings.cookie_secure,
            same_site="lax",
        )
        app.state.oauth = build_oauth(oidc_settings)
        app.include_router(auth_routes.router)
        logger.info("OIDC SSO enabled (issuer=%s)", oidc_settings.issuer)
    else:
        logger.info("OIDC SSO not configured; dashboard uses token auth only")

    app.include_router(stats.router, prefix="/api/stats", tags=["stats"])
    app.include_router(runs.router, prefix="/api/runs", tags=["runs"])
    app.include_router(ground_truth.router, prefix="/api/gt", tags=["ground_truth"])
    app.include_router(agent_definitions.router, prefix="/api/definitions", tags=["definitions"])
    app.include_router(model_metadata.router, prefix="/api/model_metadata", tags=["model_metadata"])
    app.include_router(registry.router, tags=["registry_proxy"])

    @app.get("/health")
    def health() -> dict[str, str]:
        # Liveness: the HTTP server is up and the event loop is responsive.
        # Returns 200 even during the backgrounded slow-startup window
        # (alembic migrations, specimen sync, registry construction) so that
        # kubelet doesn't kill the pod mid-init. Readiness — "is this pod
        # ready to receive Service traffic?" — lives at /readyz below.
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz(response: Response) -> dict[str, str]:
        # Readiness: backgrounded startup task has finished (specimens synced,
        # registry built, grader supervisor up). Backed by kubelet's
        # readinessProbe in deployment.yaml; the Service won't route traffic
        # to the pod until this returns 200.
        if getattr(app.state, "startup_complete", False):
            return {"status": "ready"}
        response.status_code = 503
        return {"status": "starting"}

    @app.exception_handler(Exception)
    async def debug_exception_handler(request: Request, exc: Exception) -> PlainTextResponse:
        return PlainTextResponse(
            content="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)), status_code=500
        )

    if static_dir and static_dir.exists():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")

    return app


def default_deps(host: str = "127.0.0.1", port: int = 8000) -> BackendDeps:
    config = load_config_from_env()
    return BackendDeps(
        config=config,
        registry_proxy_config=get_registry_proxy_config(),
        backend_url=config.backend_url,
        grader_model=config.grader_model,
        host=host,
        port=port,
    )
