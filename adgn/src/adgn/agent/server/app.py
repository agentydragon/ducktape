from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from datetime import datetime
import logging
import os
from pathlib import Path
from uuid import UUID

from fastapi import Body, FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastmcp.mcp_config import MCPConfig, MCPServerTypes
from pydantic import BaseModel
import uvicorn

from adgn.agent.handler import AbortTurnDecision, ContinueDecision
from adgn.agent.models.proposal_status import ProposalStatus
from adgn.agent.persist import AgentMetadata, RunRow
from adgn.agent.persist.events import EventRecord
from adgn.agent.persist.sqlite import SQLitePersistence
from adgn.agent.presets import AgentPreset, discover_presets
from adgn.agent.runtime.auto_attach import DEFAULT_AUTO_SERVER_NAMES
from adgn.agent.runtime.container import default_client_factory
from adgn.agent.runtime.registry import AgentRegistry
from adgn.agent.server.agents_ws import AgentsWSHub, register_agents_ws
from adgn.agent.server.protocol import Snapshot
from adgn.agent.server.runtime import AgentSession
from adgn.agent.server.status_shared import (
    AgentStatusCore,
    build_agent_status_core,
)
from adgn.agent.server.ws import register_ws
from adgn.mcp._shared.types import SimpleOk
from adgn.mcp.approval_policy.clients import PolicyApproverClient
from adgn.mcp.approval_policy.server import (
    ApproveProposalArgs,
    RejectProposalArgs,
)

# (runtime container constants used only in shared status builder)
import docker  # type: ignore

PROTOCOL_VERSION = "1.0.0"
DEFAULT_MODEL = os.getenv("OPENAI_MODEL", "o4-mini")

logger = logging.getLogger(__name__)


# Request/Response models (module-level to avoid nested classes)
class CreateAgentBody(BaseModel):
    preset: str
    system: str | None = None


class PatchAgentMcpBody(BaseModel):
    mcp_config: MCPConfig | None = None
    # When provided we merge into MCPConfig (name->MCPConfig with one or more servers)
    attach: dict[str, MCPConfig] | None = None
    detach: list[str] | None = None


class AgentDescriptor(BaseModel):
    id: str
    created_at: datetime
    mcp_config: MCPConfig
    metadata: AgentMetadata
    live: bool
    working: bool
    last_updated: datetime | None = None


class AgentsList(BaseModel):
    agents: list[AgentDescriptor]


class CreateAgentResult(BaseModel):
    id: str


class DeleteAgentResult(BaseModel):
    ok: bool
    error: str | None = None


class PatchAgentMcpResult(BaseModel):
    id: str
    mcp_config: MCPConfig


class AttachOneMcpBody(BaseModel):
    name: str
    spec: MCPServerTypes


class DetachOneMcpBody(BaseModel):
    name: str


class AgentInfo(BaseModel):
    agent: AgentDescriptor | None
    live: bool


# Typed status bundle (references component models defined above)
class AgentStatus(AgentStatusCore):
    """HTTP response model for agent status; mirrors shared core schema."""


class SetPolicyBody(BaseModel):
    content: str
    proposal_id: str | None = None


class ApproveBody(BaseModel):
    call_id: str


class PromptBody(BaseModel):
    text: str


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


class PresetInfo(BaseModel):
    preset: AgentPreset | None


# Boot outcome (explicit start of an existing agent)
class BootAgentResult(BaseModel):
    ok: bool
    error: str | None = None


## WebSocket message models moved to ws.py


# Factory to create an isolated app with fresh manager/session


def create_app(
    *,
    require_static_assets: bool = True,
) -> FastAPI:
    app = FastAPI()
    STATIC_DIR = Path(__file__).with_name("static")

    def _mount_static(path: str, directory: Path, name: str) -> None:
        if not directory.exists():
            if require_static_assets:
                raise RuntimeError(
                    f"Static directory missing: {directory}. Build MiniCodex UI assets before running."
                )
            logger.warning(
                "Skipping mount for missing static directory",
                extra={"path": path, "directory": str(directory)},
            )
            return
        app.mount(path, StaticFiles(directory=directory, check_dir=True), name=name)

    _mount_static("/static", STATIC_DIR, "static")
    _mount_static("/assets", STATIC_DIR / "assets", "assets")

    # Optional CORS (for dev cross-origin fetches). Disabled by default.
    # Enable by setting ADGN_UI_CORS_ORIGINS to a comma-separated list or "*".
    cors_env = os.getenv("ADGN_UI_CORS_ORIGINS")
    if cors_env:
        origins = (
            [o.strip() for o in cors_env.split(",") if o.strip()] if cors_env != "*" else ["*"]
        )
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
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
    # Initialize the agents WS hub explicitly (no lazy creation in route registrar)
    app.state.agents_ws_hub = AgentsWSHub(app)

    # (continued below)

    @app.on_event("startup")
    async def _on_startup() -> None:
        # Enter the app-level async stack for resource management
        await app.state.stack.__aenter__()
        index_path = STATIC_DIR / "index.html"
        logger.info(
            "server startup",
            extra={
                "static_dir": str(STATIC_DIR),
                "index_exists": index_path.exists(),
                "index_path": str(index_path),
            },
        )

        # Ensure persistence schema (generic agent store) — fail startup on error
        await app.state.persistence.ensure_schema()
        logger.info("persistence ready", extra={"db_path": str(db_path)})

        # Multi-agent: agents should be created via API after startup
        app.state.ready.set()

    @app.on_event("shutdown")
    async def _on_shutdown() -> None:
        """Flush UI events and close all containers via registry actor paths."""
        # Close app-managed async resources first
        try:
            await app.state.stack.aclose()
        except Exception:
            # Continue shutdown on errors; they will be logged by the caller
            pass
        for container in app.state.registry.list():
            if container.ui:
                await container.ui.manager.flush()
        await app.state.registry.close_all()

    @app.get("/", response_model=None)
    async def index() -> Response:
        # Serve built Svelte app
        file_path = STATIC_DIR / "index.html"
        if not file_path.exists():
            if require_static_assets:
                raise RuntimeError(f"Missing UI file: {file_path}")
            return Response(
                content="MiniCodex UI assets not built",
                media_type="text/plain",
                status_code=200,
            )
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

    @app.get("/api/agents", response_model=AgentsList)
    async def api_list_agents() -> AgentsList:
        rows = await app.state.persistence.list_agents()
        live = {c.agent_id: c for c in app.state.registry.list()}
        working_ids = {
            cid
            for cid, c in live.items()
            if (c.session is not None and c.session.active_run is not None)
        }
        last_map = await app.state.persistence.list_agents_last_activity()
        # Build enriched list and sort by last activity desc (fallback to created_at)
        items: list[tuple[str, datetime, AgentDescriptor]] = []
        for r in rows:
            last_ts = last_map.get(r.id) or r.created_at
            items.append(
                (
                    r.id,
                    last_ts,
                    AgentDescriptor(
                        id=r.id,
                        created_at=r.created_at,
                        mcp_config=r.mcp_config,
                        metadata=r.metadata,
                        live=r.id in live,
                        working=r.id in working_ids,
                        last_updated=last_ts,
                    ),
                )
            )
        items.sort(key=lambda t: t[1], reverse=True)
        return AgentsList(agents=[p for _, _, p in items])

    @app.post("/api/agents", response_model=CreateAgentResult)
    async def api_create_agent(create: CreateAgentBody = Body(...)) -> CreateAgentResult:
        # Lookup preset; combine with optional override system
        ps = discover_presets(os.getenv("ADGN_AGENT_PRESETS_DIR"))
        p = ps.get(create.preset)
        if p is None:
            raise HTTPException(status_code=404, detail="unknown_preset")
        # Inline conversion of preset specs to typed MCPConfig
        typed_cfg = MCPConfig.model_validate({"mcpServers": p.specs or {}})
        # Store only internal preset info (no freeform metadata)
        metadata = AgentMetadata(preset=create.preset)
        # Persist JSON specs and metadata
        agent_id = await app.state.persistence.create_agent(mcp_config=typed_cfg, metadata=metadata)

        # Do not persist policy; engine is the single source of truth
        # Notify general WS subscribers (hub presence required)
        hub = app.state.agents_ws_hub
        await hub.broadcast_agent_created(agent_id)
        await hub.broadcast_agent_status(agent_id=agent_id, live=False, active_run_id=None)
        return CreateAgentResult(id=agent_id)

    # Explicit boot endpoint: asserts per-agent volumes/policy exist, then starts live container
    @app.post("/api/agents/{agent_id}/boot", response_model=BootAgentResult)
    async def api_boot_agent(agent_id: str) -> BootAgentResult:
        row = await app.state.persistence.get_agent(agent_id)
        if row is None:
            return BootAgentResult(ok=False, error="agent_not_found")
        # No volume checks; policy presence validated on startup via engine/persistence
        if app.state.registry.get(agent_id) is not None:
            return BootAgentResult(ok=True)
        await app.state.registry.create(agent_id, row.mcp_config, with_ui=True)
        return BootAgentResult(ok=True)

    @app.delete("/api/agents/{agent_id}", response_model=DeleteAgentResult)
    async def api_delete_agent(agent_id: str) -> DeleteAgentResult:
        # Look up live container and persisted agent row
        container = app.state.registry.get(agent_id)
        row = await app.state.persistence.get_agent(agent_id)
        if container is None and row is None:
            return DeleteAgentResult(ok=False, error="not_found")
        # If live, close deterministically (cancels run, waits idle, drains persistence)
        if container is not None:
            result = await container.close()
            # Remove closed container from registry regardless of drain outcome
            app.state.registry.remove(agent_id)
            # If drain failed, abort purge and return error
            if not (isinstance(result, dict) and result.get("drained", True)):
                return DeleteAgentResult(ok=False, error="drain_failed")
        # Always purge persisted records when present
        if row is not None:
            await app.state.persistence.delete_agent(agent_id)
        # Notify general WS subscribers (hub presence required)
        hub = app.state.agents_ws_hub
        await hub.broadcast_agent_status(agent_id=agent_id, live=False, active_run_id=None)
        await hub.broadcast_agent_deleted(agent_id)
        return DeleteAgentResult(ok=True)

    @app.patch("/api/agents/{agent_id}/mcp", response_model=PatchAgentMcpResult)
    async def api_patch_agent_mcp(
        agent_id: str, patch: PatchAgentMcpBody = Body(...)
    ) -> PatchAgentMcpResult:
        # Validate detach does not target default auto-attached servers
        if patch.detach:
            forbidden = set(DEFAULT_AUTO_SERVER_NAMES) | {"compositor"}
            bad = [n for n in patch.detach if n in forbidden]
            if bad:
                raise HTTPException(
                    status_code=400, detail={"error": "cannot_detach_auto", "servers": bad}
                )
        # Persist desired state first
        if patch.mcp_config is not None:
            await app.state.persistence.update_agent_specs(agent_id, mcp_config=patch.mcp_config)
            persisted_cfg = patch.mcp_config
        else:
            persisted_cfg = await app.state.persistence.patch_agent_specs(
                agent_id, attach=patch.attach, detach=patch.detach
            )
        # Apply live changes if container exists
        container = app.state.registry.get(agent_id)
        if container is not None:
            if patch.mcp_config is not None:
                await container.reconfigure_mcp(mcp_config=patch.mcp_config)
            else:
                await container.reconfigure_mcp(attach=patch.attach or {}, detach=patch.detach)
        return PatchAgentMcpResult(id=agent_id, mcp_config=persisted_cfg)

    @app.post("/api/agents/{agent_id}/mcp/attach", response_model=PatchAgentMcpResult)
    async def api_attach_agent_mcp(
        agent_id: str, body: AttachOneMcpBody = Body(...)
    ) -> PatchAgentMcpResult:
        row = await app.state.persistence.get_agent(agent_id)
        if row is None:
            raise HTTPException(status_code=404, detail="agent not found")
        # Persist attach of a single server
        attach_cfg = MCPConfig.model_validate({"mcpServers": {body.name: body.spec}})
        persisted_cfg = await app.state.persistence.patch_agent_specs(
            agent_id, attach={"single": attach_cfg}, detach=[]
        )
        # Live apply if running
        container = app.state.registry.get(agent_id)
        if container is not None:
            await container.attach_mcp(body.name, body.spec)
        return PatchAgentMcpResult(id=agent_id, mcp_config=persisted_cfg)

    @app.post("/api/agents/{agent_id}/mcp/detach", response_model=PatchAgentMcpResult)
    async def api_detach_agent_mcp(
        agent_id: str, body: DetachOneMcpBody = Body(...)
    ) -> PatchAgentMcpResult:
        row = await app.state.persistence.get_agent(agent_id)
        if row is None:
            raise HTTPException(status_code=404, detail="agent not found")
        # Validate requested detach names are allowed
        forbidden = set(DEFAULT_AUTO_SERVER_NAMES) | {"compositor"}
        if body.name in forbidden:
            raise HTTPException(
                status_code=400, detail={"error": "cannot_detach_auto", "servers": [body.name]}
            )
        # Persist detach (single)
        persisted_cfg = await app.state.persistence.patch_agent_specs(
            agent_id, attach={}, detach=[body.name]
        )
        # Live apply if running
        container = app.state.registry.get(agent_id)
        if container is not None:
            await container.detach_mcp(body.name)
        return PatchAgentMcpResult(id=agent_id, mcp_config=persisted_cfg)

    @app.get("/api/agents/{agent_id}", response_model=AgentInfo)
    async def api_get_agent(agent_id: str) -> AgentInfo:
        row = await app.state.persistence.get_agent(agent_id)
        live = app.state.registry.get(agent_id) is not None
        if row is None:
            return AgentInfo(agent=None, live=live)
        working = False
        cont = app.state.registry.get(agent_id)
        if cont is not None and cont.session is not None and cont.session.active_run:
            working = True
        return AgentInfo(
            agent=AgentDescriptor(
                id=row.id,
                created_at=row.created_at,
                mcp_config=row.mcp_config,
                metadata=row.metadata,
                live=live,
                working=working,
                last_updated=None,
            ),
            live=live,
        )

    # Pull current snapshot for an agent
    @app.get("/api/agents/{agent_id}/snapshot", response_model=Snapshot)
    async def api_get_snapshot(agent_id: str) -> Snapshot:
        try:
            container = await app.state.registry.ensure_live(agent_id, with_ui=True)
        except KeyError as e:
            raise HTTPException(status_code=404, detail="agent not found") from e
        sess: AgentSession | None = container.session
        if sess is None:
            raise HTTPException(status_code=500, detail="no session")
        sampling = await container.sampling_snapshot()
        return await sess.build_snapshot(sampling=sampling)

    @app.get("/api/agents/{agent_id}/status", response_model=AgentStatus)
    async def api_agent_status(agent_id: str) -> AgentStatus:
        core = await build_agent_status_core(app, agent_id)
        # Re-validate into HTTP schema; dump as JSON-like to coerce enums/inner models
        return AgentStatus(**core.model_dump(mode="json"))

    # Withdraw is expressed as reject in API; see /proposals/{id}/reject

    # --- Policy: set active content (optionally from proposal) ---
    @app.post("/api/agents/{agent_id}/policy", response_model=SimpleOk)
    async def api_set_policy(agent_id: str, body: SetPolicyBody = Body(...)) -> SimpleOk:
        # Route through the agent container's policy approver client
        try:
            container = await app.state.registry.ensure_live(agent_id, with_ui=True)
        except KeyError as e:
            raise HTTPException(status_code=404, detail="agent_not_found") from e
        approver_client: PolicyApproverClient = container.policy_approver
        try:
            await approver_client.set_policy_text(body.content)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"policy_set_failed: {e}") from e
        # If proposal id provided, delete it from store
        if body.proposal_id:
            await app.state.persistence.delete_policy_proposal(agent_id, body.proposal_id)
        # Push snapshot (do not swallow errors)
        if container.ui is not None:
            sess = container.session
            if sess is None:
                raise HTTPException(status_code=500, detail="no_session")
            await _send_snapshot_latest(container, sess)
        return SimpleOk(ok=True)

    # --- Approvals (user decisions) ---
    @app.post("/api/agents/{agent_id}/approve", response_model=SimpleOk)
    async def api_approve(agent_id: str, body: ApproveBody = Body(...)) -> SimpleOk:
        try:
            container = await app.state.registry.ensure_live(agent_id, with_ui=True)
        except KeyError as e:
            raise HTTPException(status_code=404, detail="agent_not_found") from e
        sess = container.session
        if sess is None:
            raise HTTPException(status_code=500, detail="no_session")
        if body.call_id not in sess.approval_hub._requests:
            raise HTTPException(status_code=404, detail="unknown_call")
        sess.approval_hub.resolve(body.call_id, ContinueDecision())
        if container.ui is not None:
            await _send_snapshot_latest(container, sess)
        return SimpleOk(ok=True)

    @app.post("/api/agents/{agent_id}/deny_continue", response_model=SimpleOk)
    async def api_deny_continue(agent_id: str, body: ApproveBody = Body(...)) -> SimpleOk:
        try:
            container = await app.state.registry.ensure_live(agent_id, with_ui=True)
        except KeyError as e:
            raise HTTPException(status_code=404, detail="agent_not_found") from e
        sess = container.session
        if sess is None:
            raise HTTPException(status_code=500, detail="no_session")
        if body.call_id not in sess.approval_hub._requests:
            raise HTTPException(status_code=404, detail="unknown_call")
        # Map deny-continue to abort semantics at the middleware boundary
        sess.approval_hub.resolve(
            body.call_id, AbortTurnDecision(reason=f"User denied: {body.call_id}")
        )
        if container.ui is not None:
            await _send_snapshot_latest(container, sess)
        return SimpleOk(ok=True)

    # --- Policy proposals: approve/reject -----------------------------------
    @app.post("/api/agents/{agent_id}/proposals/{proposal_id}/approve", response_model=SimpleOk)
    async def api_approve_proposal(agent_id: str, proposal_id: str) -> SimpleOk:
        try:
            container = await app.state.registry.ensure_live(agent_id, with_ui=True)
        except KeyError as e:
            raise HTTPException(status_code=404, detail="agent_not_found") from e
        approver_client: PolicyApproverClient = container.policy_approver
        try:
            await approver_client.approve_proposal(ApproveProposalArgs(id=proposal_id))
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"approve_proposal_failed: {e}") from e
        await app.state.persistence.approve_policy_proposal(agent_id, proposal_id)
        # Push snapshot update to UIs (do not swallow errors)
        if container.ui is not None:
            sess = container.session
            if sess is None:
                raise HTTPException(status_code=500, detail="no_session")
            await _send_snapshot_latest(container, sess)
        return SimpleOk(ok=True)

    @app.post("/api/agents/{agent_id}/proposals/{proposal_id}/reject", response_model=SimpleOk)
    async def api_reject_proposal(agent_id: str, proposal_id: str) -> SimpleOk:
        try:
            container = await app.state.registry.ensure_live(agent_id, with_ui=True)
        except KeyError as e:
            raise HTTPException(status_code=404, detail="agent_not_found") from e
        approver_client: PolicyApproverClient = container.policy_approver
        try:
            await approver_client.reject_proposal(RejectProposalArgs(id=proposal_id))
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"reject_proposal_failed: {e}") from e
        return SimpleOk(ok=True)

    @app.post("/api/agents/{agent_id}/deny_abort", response_model=SimpleOk)
    async def api_deny_abort(agent_id: str, body: ApproveBody = Body(...)) -> SimpleOk:
        try:
            container = await app.state.registry.ensure_live(agent_id, with_ui=True)
        except KeyError as e:
            raise HTTPException(status_code=404, detail="agent_not_found") from e
        sess = container.session
        if sess is None:
            raise HTTPException(status_code=500, detail="no_session")
        if body.call_id not in sess.approval_hub._requests:
            raise HTTPException(status_code=404, detail="unknown_call")
        sess.approval_hub.resolve(body.call_id, AbortTurnDecision(reason="ui_deny"))
        if container.ui is not None:
            await _send_snapshot_latest(container, sess)
        return SimpleOk(ok=True)

    # --- Prompt/Abort ---
    @app.post("/api/agents/{agent_id}/prompt", response_model=SimpleOk)
    async def api_prompt(agent_id: str, body: PromptBody = Body(...)) -> SimpleOk:
        try:
            container = await app.state.registry.ensure_live(agent_id, with_ui=True)
        except KeyError as e:
            logger.info("api_prompt: agent_not_found", extra={"agent_id": agent_id})
            raise HTTPException(status_code=404, detail="agent_not_found") from e
        sess = container.session
        if sess is None:
            logger.info("api_prompt: no session", extra={"agent_id": agent_id})
            raise HTTPException(status_code=500, detail="no_session")
        # Start run (session schedules the long task internally)
        await sess.run(body.text)
        logger.info("api_prompt: started", extra={"agent_id": agent_id, "text": body.text[:64]})
        return SimpleOk(ok=True)

    @app.post("/api/agents/{agent_id}/abort", response_model=SimpleOk)
    async def api_abort(agent_id: str) -> SimpleOk:
        try:
            container = await app.state.registry.ensure_live(agent_id, with_ui=True)
        except KeyError as e:
            raise HTTPException(status_code=404, detail="agent_not_found") from e
        sess = container.session
        if sess is None:
            raise HTTPException(status_code=500, detail="no_session")
        if sess.active_run is not None:
            await sess.cancel_active_run()
            return SimpleOk(ok=True)
        raise HTTPException(status_code=400, detail="not_running")

    @app.get("/api/runs", response_model=RunsList)
    async def api_list_runs(agent_id: str | None = None, limit: int = 50) -> RunsList:
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
    async def api_list_proposals(agent_id: str) -> ProposalsList:
        rows = await app.state.persistence.list_policy_proposals(agent_id)
        items = [
            ProposalRow(
                id=rec.id,
                status=ProposalStatus(rec.status),
                created_at=rec.created_at,
                decided_at=rec.decided_at,
            )
            for rec in rows
        ]
        return ProposalsList(proposals=items)

    @app.get("/api/agents/{agent_id}/proposals/{proposal_id}", response_model=ProposalContent)
    async def api_get_proposal(agent_id: str, proposal_id: str) -> ProposalContent:
        rec = await app.state.persistence.get_policy_proposal(agent_id, proposal_id)
        if rec is None:
            raise HTTPException(status_code=404, detail="proposal_not_found")
        return ProposalContent(
            id=rec.id,
            content=rec.content,
            status=ProposalStatus(rec.status),
            created_at=rec.created_at,
            decided_at=rec.decided_at,
        )

    # -----------------------
    # Presets API
    # -----------------------

    def _load_presets() -> dict[str, AgentPreset]:
        return discover_presets(os.getenv("ADGN_AGENT_PRESETS_DIR"))

    # Typed response models for presets listing
    class PresetSummary(BaseModel):
        name: str
        description: str | None = None

    class PresetsList(BaseModel):
        presets: list[PresetSummary]

    @app.get("/api/presets", response_model=PresetsList)
    async def api_list_presets() -> PresetsList:
        ps = _load_presets()
        items: list[PresetSummary] = [
            PresetSummary(name=name, description=p.description or None) for name, p in ps.items()
        ]
        return PresetsList(presets=items)

    @app.get("/api/presets/{name}", response_model=PresetInfo)
    async def api_get_preset(name: str) -> PresetInfo:
        ps = _load_presets()
        p = ps.get(name)
        return PresetInfo(preset=p if p else None)

    # Register websocket routes
    register_ws(app)
    register_agents_ws(app)

    return app


def run_uvicorn(host: str = "127.0.0.1", port: int = 8765) -> None:
    uvicorn.run(
        "adgn.agent.server.app:create_app",
        host=host,
        port=port,
        log_level="info",
        factory=True,
    )


# Small helpers to dedupe snapshot send pattern
async def _send_snapshot(container, sess, sampling=None) -> None:
    if container.ui is None:
        raise RuntimeError("UI manager not available for snapshot send")
    await container.ui.manager.send_payload(await sess.build_snapshot(sampling=sampling))


async def _send_snapshot_latest(container, sess) -> None:
    sampling = await container.sampling_snapshot()
    await _send_snapshot(container, sess, sampling=sampling)
