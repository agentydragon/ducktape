from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from contextlib import AsyncExitStack
from pathlib import Path

import aiodocker
import uvicorn
from fastapi import FastAPI, FastAPI as SubApp, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastmcp.mcp_config import MCPConfig

from agent_server.mcp_bridge.auth import TokensConfig
from agent_server.mcp_bridge.compositor_factory import create_global_compositor
from agent_server.mcp_bridge.registry import InfrastructureRegistry
from agent_server.persist.sqlite import SQLitePersistence
from agent_server.runtime.registry import AgentRegistry
from agent_server.server.mcp_routing import TOKEN_TABLE, MCPRoutingMiddleware
from openai_utils.client_factory import build_client
from openai_utils.model import OpenAIModelProto

DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.1-codex-mini")

logger = logging.getLogger(__name__)

# Static directory for UI assets
STATIC_DIR = Path(__file__).with_name("static")


def default_client_factory(model: str) -> OpenAIModelProto:
    return build_client(model, enable_debug_logging=True)


def create_app(*, require_static_assets: bool = True) -> FastAPI:
    """Factory to create an isolated app with fresh manager/session."""
    app = FastAPI()

    # Initialize state variables to None (mypy infers types from usage)
    app.state.mcp_registry = None
    app.state.global_compositor = None

    def _mount_static(path: str, directory: Path, name: str) -> None:
        if not directory.exists():
            if require_static_assets:
                raise RuntimeError(f"Static directory missing: {directory}. Build Agent UI assets before running.")
            logger.warning(
                "Skipping mount for missing static directory", extra={"path": path, "directory": str(directory)}
            )
            return
        app.mount(path, StaticFiles(directory=directory, check_dir=True), name=name)

    _mount_static("/static", STATIC_DIR, "static")
    _mount_static("/assets", STATIC_DIR / "assets", "assets")

    # Optional CORS (for dev cross-origin fetches). Disabled by default.
    # Enable by setting ADGN_UI_CORS_ORIGINS to a comma-separated list or "*".
    cors_env = os.getenv("ADGN_UI_CORS_ORIGINS")
    if cors_env:
        origins = [o.strip() for o in cors_env.split(",") if o.strip()] if cors_env != "*" else ["*"]
        app.add_middleware(
            CORSMiddleware, allow_origins=origins, allow_credentials=True, allow_methods=["*"], allow_headers=["*"]
        )

    # Readiness event so async tests can await startup deterministically
    app.state.ready = asyncio.Event()
    # Async resource stack for long-lived clients created by the app
    app.state.stack = AsyncExitStack()
    # Wire SQLite persistence at creation; ensure schema during startup
    raw_db_path = os.getenv("ADGN_AGENT_DB_PATH")
    db_path = Path(raw_db_path) if raw_db_path else Path("logs") / "agent.sqlite"
    db_path = db_path.expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    app.state.persistence = SQLitePersistence(db_path)
    # Construct async Docker client and pass through to the registry/containers
    async_docker_client = aiodocker.Docker()
    app.state.registry = AgentRegistry(
        persistence=app.state.persistence,
        model=DEFAULT_MODEL,
        client_factory=default_client_factory,
        async_docker_client=async_docker_client,
    )

    # (continued below)

    @app.on_event("startup")
    async def _on_startup() -> None:
        # Enter the app-level async stack for resource management
        await app.state.stack.__aenter__()
        index_path = STATIC_DIR / "index.html"
        logger.info(
            "server startup",
            extra={"static_dir": str(STATIC_DIR), "index_exists": index_path.exists(), "index_path": str(index_path)},
        )

        # Ensure persistence schema (generic agent store) — fail startup on error
        await app.state.persistence.ensure_schema()
        logger.info("persistence ready", extra={"db_path": str(db_path)})

        # Create infrastructure registry for Phase 5 two-compositor architecture
        app.state.mcp_registry = InfrastructureRegistry(
            persistence=app.state.persistence,
            model=DEFAULT_MODEL,
            client_factory=default_client_factory,
            async_docker_client=async_docker_client,
            mcp_config=MCPConfig(),
            initial_policy=None,
        )

        # Create global compositor with agents management server
        app.state.global_compositor = await create_global_compositor(registry=app.state.mcp_registry)
        logger.info("Global compositor created with agents management server")

        # Load tokens and create external agents at startup
        config = TokensConfig.from_yaml_file()
        for agent_id in config.agent_tokens().values():
            await app.state.mcp_registry.create_external_agent(agent_id)
            logger.info(f"Created external agent from token: {agent_id}")

        # Multi-agent: agents should be created via API after startup
        app.state.ready.set()

    @app.on_event("shutdown")
    async def _on_shutdown() -> None:
        """Flush UI events and close all containers via registry actor paths."""
        # Close app-managed async resources first
        with contextlib.suppress(Exception):
            await app.state.stack.aclose()
            # Continue shutdown on errors; they will be logged by the caller

        # Shutdown all agents via mcp_registry (Phase 5 path)
        if app.state.mcp_registry is not None:
            await app.state.mcp_registry.shutdown_all()

        # Close global compositor to cleanup mounted servers
        if app.state.global_compositor is not None:
            with contextlib.suppress(Exception):
                await app.state.global_compositor.close()

        # Legacy registry path for backwards compatibility
        for container in app.state.registry.list():
            # Flush legacy UI manager
            if container._ui_manager:
                await container._ui_manager.flush()
        await app.state.registry.close_all()

        # Close async Docker client
        with contextlib.suppress(Exception):
            await async_docker_client.close()

    @app.get("/", response_model=None)
    async def index() -> Response:
        # Serve built Svelte app
        file_path = STATIC_DIR / "index.html"
        if not file_path.exists():
            if require_static_assets:
                raise RuntimeError(f"Missing UI file: {file_path}")
            return Response(content="Agent UI assets not built", media_type="text/plain", status_code=200)
        return FileResponse(file_path)

    # Mount MCP routing endpoint
    # Note: The global_compositor is created during startup, so this uses a lazy sub-app
    mcp_sub_app = SubApp()

    @mcp_sub_app.middleware("http")
    async def mcp_routing_middleware_wrapper(request, call_next):
        """Wrapper to apply MCP routing middleware after startup."""
        if app.state.global_compositor is None:
            return Response(content="Server not ready", status_code=503)

        middleware = MCPRoutingMiddleware(
            app=mcp_sub_app,
            token_table=TOKEN_TABLE,
            registry=app.state.registry,
            agents_server=app.state.global_compositor,
        )
        return await middleware.dispatch(request, call_next)

    app.mount("/mcp", mcp_sub_app)
    logger.info("MCP routing endpoint mounted at /mcp")

    return app


def run_uvicorn(host: str = "127.0.0.1", port: int = 8765) -> None:
    uvicorn.run("agent_server.server.app:create_app", host=host, port=port, log_level="info", factory=True)
