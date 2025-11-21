from __future__ import annotations

import asyncio
import contextlib
from contextlib import AsyncExitStack
from datetime import datetime
import logging
import os
from pathlib import Path
from uuid import UUID

# (runtime container constants used only in shared status builder)
import docker  # type: ignore
from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastmcp.mcp_config import MCPConfig
from pydantic import BaseModel
import uvicorn

from adgn.agent.models.proposal_status import ProposalStatus
from adgn.agent.persist import RunRow
from adgn.agent.persist.events import EventRecord
from adgn.agent.persist.sqlite import SQLitePersistence
from adgn.agent.runtime.registry import AgentRegistry
from adgn.agent.server.exceptions import (
    AgentNotFoundError,
    AgentSessionNotReadyError,
    ApprovalNotFoundError,
    PolicyOperationError,
)
from adgn.agent.server.mcp_routing import TOKEN_TABLE, MCPRoutingMiddleware
from adgn.agent.server.protocol import Snapshot
from adgn.agent.server.runtime import AgentSession
from adgn.agent.server.status_shared import AgentStatusCore, build_agent_status_core
from adgn.agent.types import AgentID
from adgn.openai_utils.client_factory import build_client
from adgn.openai_utils.model import OpenAIModelProto

PROTOCOL_VERSION = "1.0.0"
DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "o4-mini")

logger = logging.getLogger(__name__)

# Static directory for UI assets
STATIC_DIR = Path(__file__).with_name("static")


def default_client_factory(model: str) -> OpenAIModelProto:
    """Default LLM client factory."""
    return build_client(model, enable_debug_logging=True)


# Request/Response models (module-level to avoid nested classes)


# Typed status bundle (references component models defined above)
class AgentStatus(AgentStatusCore):
    """HTTP response model for agent status; mirrors shared core schema."""


class RunsList(BaseModel):
    runs: list[RunRow]


class RunInfo(BaseModel):
    run: RunRow | None


class RunEvents(BaseModel):
    events: list[EventRecord]

    # Proposals API (list and read content)


class ProposalRow(BaseModel):
    id: str
    status: ProposalStatus
    created_at: datetime
    decided_at: datetime | None = None


class ProposalsList(BaseModel):
    proposals: list[ProposalRow]


class ProposalContent(BaseModel):
    id: str
    content: str
    status: ProposalStatus
    created_at: datetime
    decided_at: datetime | None = None


## WebSocket message models moved to ws.py


# Factory to create an isolated app with fresh manager/session


def create_app(*, require_static_assets: bool = True) -> FastAPI:
    app = FastAPI()

    # Register exception handlers for domain exceptions
    @app.exception_handler(AgentNotFoundError)
    async def handle_agent_not_found(request, exc: AgentNotFoundError):
        raise HTTPException(status_code=404, detail="agent_not_found") from exc

    @app.exception_handler(AgentSessionNotReadyError)
    async def handle_session_not_ready(request, exc: AgentSessionNotReadyError):
        raise HTTPException(status_code=500, detail="no_session") from exc

    @app.exception_handler(PolicyOperationError)
    async def handle_policy_operation_error(request, exc: PolicyOperationError):
        raise HTTPException(status_code=400, detail=f"policy_operation_error: {exc.reason}") from exc

    @app.exception_handler(ApprovalNotFoundError)
    async def handle_approval_not_found(request, exc: ApprovalNotFoundError):
        raise HTTPException(status_code=404, detail="approval_not_found") from exc

    def _mount_static(path: str, directory: Path, name: str) -> None:
        if not directory.exists():
            if require_static_assets:
                raise RuntimeError(f"Static directory missing: {directory}. Build MiniCodex UI assets before running.")
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
    # Construct a single Docker client and pass through to the registry/containers
    app.state.docker_client = docker.from_env()
    app.state.registry = AgentRegistry(
        persistence=app.state.persistence,
        model=DEFAULT_MODEL,
        client_factory=default_client_factory,
        docker_client=app.state.docker_client,
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

        # Create agents management server for MCP routing
        # Import here to avoid circular dependency with registry setup
        from adgn.agent.mcp_bridge.server import InfrastructureRegistry  # noqa: PLC0415

        # Create minimal infrastructure registry for agents server
        # Note: This is a simplified setup - in production, you'd want proper registry management
        app.state.mcp_registry = InfrastructureRegistry(
            persistence=app.state.persistence,
            docker_client=app.state.docker_client,
            mcp_config=MCPConfig(servers={}),
            initial_policy=None,
        )

        # Feature flag: Use new compositor architecture or old monolithic server
        use_compositor = os.getenv("ADGN_USE_COMPOSITOR_BRIDGE", "false").lower() == "true"

        if use_compositor:
            # New: Create global compositor with two-level architecture
            from adgn.agent.mcp_bridge.compositor_factory import create_global_compositor  # noqa: PLC0415

            # TODO: Figure out gateway_client setup for resources server
            # For now, pass None - standard infrastructure servers won't be mounted
            gateway_client = None

            app.state.agents_server = await create_global_compositor(
                registry=app.state.mcp_registry, gateway_client=gateway_client
            )
            logger.info("agents management compositor created (new architecture)")
        else:
            # Old: Use monolithic agents server
            from adgn.agent.mcp_bridge.servers.agents import make_agents_server  # noqa: PLC0415

            app.state.agents_server = await make_agents_server(app.state.mcp_registry)
            logger.info("agents management server created (legacy architecture)")

        # Multi-agent: agents should be created via API after startup
        app.state.ready.set()

    @app.on_event("shutdown")
    async def _on_shutdown() -> None:
        """Flush UI events and close all containers via registry actor paths."""
        # Close app-managed async resources first
        with contextlib.suppress(Exception):
            await app.state.stack.aclose()
            # Continue shutdown on errors; they will be logged by the caller
        for container in app.state.registry.list():
            # Flush legacy UI manager
            if container._ui_manager:
                await container._ui_manager.flush()
        await app.state.registry.close_all()

    # Helper functions to reduce boilerplate
    async def get_container(agent_id: AgentID):
        """Get live container for agent, raising AgentNotFoundError if missing."""
        try:
            return await app.state.registry.ensure_live(agent_id, with_ui=True)
        except KeyError as e:
            raise AgentNotFoundError(agent_id) from e

    def get_session(container, agent_id: AgentID) -> AgentSession:
        """Get session from container, raising AgentSessionNotReadyError if not initialized."""
        if container.runtime.session is None:
            raise AgentSessionNotReadyError(agent_id)
        return container.runtime.session

    @app.get("/", response_model=None)
    async def index() -> Response:
        # Serve built Svelte app
        file_path = STATIC_DIR / "index.html"
        if not file_path.exists():
            if require_static_assets:
                raise RuntimeError(f"Missing UI file: {file_path}")
            return Response(content="MiniCodex UI assets not built", media_type="text/plain", status_code=200)
        return FileResponse(file_path)

    @app.get("/vite.svg", response_model=None)
    async def vite_svg() -> Response:
        svg = STATIC_DIR / "vite.svg"
        if not svg.exists():
            if require_static_assets:
                raise RuntimeError("Missing vite.svg asset")
            return Response(content="", media_type="image/svg+xml", status_code=404)
        return FileResponse(svg)

    # -----------------------
    # Agents/Runs API (alpha)
    # -----------------------

    # No-op helper removed: direct registry.create is used where needed

    # Pull current snapshot for an agent
    @app.get("/api/agents/{agent_id}/snapshot", response_model=Snapshot)
    async def api_get_snapshot(agent_id: AgentID) -> Snapshot:
        container = await get_container(agent_id)
        sess = get_session(container, agent_id)
        sampling = await container.running.compositor.sampling_snapshot()
        return await sess.build_snapshot(sampling=sampling)

    @app.get("/api/agents/{agent_id}/status", response_model=AgentStatus)
    async def api_agent_status(agent_id: AgentID) -> AgentStatus:
        core = await build_agent_status_core(app, agent_id)
        # Re-validate into HTTP schema; dump as JSON-like to coerce enums/inner models
        return AgentStatus(**core.model_dump(mode="json"))

    @app.get("/api/runs", response_model=RunsList)
    async def api_list_runs(agent_id: AgentID | None = None, limit: int = 50) -> RunsList:
        rows = await app.state.persistence.list_runs(agent_id=agent_id, limit=limit)
        return RunsList(runs=rows)

    @app.get("/api/runs/{run_id}", response_model=RunInfo)
    async def api_get_run(run_id: UUID) -> RunInfo:
        row = await app.state.persistence.get_run(run_id)
        return RunInfo(run=row)

    @app.get("/api/runs/{run_id}/events", response_model=RunEvents)
    async def api_get_run_events(run_id: UUID) -> RunEvents:
        events = await app.state.persistence.load_events(run_id)
        return RunEvents(events=events)

    # Proposals list/content
    @app.get("/api/agents/{agent_id}/proposals", response_model=ProposalsList)
    async def api_list_proposals(agent_id: AgentID) -> ProposalsList:
        rows = await app.state.persistence.list_policy_proposals(agent_id)
        items = [
            ProposalRow(
                id=rec.id, status=ProposalStatus(rec.status), created_at=rec.created_at, decided_at=rec.decided_at
            )
            for rec in rows
        ]
        return ProposalsList(proposals=items)

    # Mount MCP routing endpoint
    # Note: The agents_server is created during startup, so this uses a lazy sub-app
    from fastapi import FastAPI as SubApp  # noqa: PLC0415

    mcp_sub_app = SubApp()

    @mcp_sub_app.middleware("http")
    async def mcp_routing_middleware_wrapper(request, call_next):
        """Wrapper to apply MCP routing middleware after startup."""
        if not hasattr(app.state, "agents_server"):
            return Response(content="Server not ready", status_code=503)

        # Create middleware instance
        middleware = MCPRoutingMiddleware(
            app=mcp_sub_app, token_table=TOKEN_TABLE, registry=app.state.registry, agents_server=app.state.agents_server
        )
        return await middleware.dispatch(request, call_next)

    app.mount("/mcp", mcp_sub_app)
    logger.info("MCP routing endpoint mounted at /mcp")

    # TODO: Register websocket routes (placeholder)
    # register_agents_ws(app)

    # TODO: Register modular channel endpoints (placeholder)
    # register_channel_endpoints(app)

    return app


def run_uvicorn(host: str = "127.0.0.1", port: int = 8765) -> None:
    uvicorn.run("adgn.agent.server.app:create_app", host=host, port=port, log_level="info", factory=True)


# Small helpers to dedupe snapshot send pattern
async def _send_snapshot(container, sess, sampling=None) -> None:
    if container._ui_manager is None:
        raise RuntimeError("UI manager not available for snapshot send")
    await container._ui_manager.send_payload(await sess.build_snapshot(sampling=sampling))


async def _send_snapshot_latest(container, sess) -> None:
    sampling = await container.running.compositor.sampling_snapshot()
    await _send_snapshot(container, sess, sampling=sampling)
