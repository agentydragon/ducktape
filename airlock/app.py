"""Airlock FastAPI application.

/healthz          — liveness probe (no auth)
/auth/config      — OIDC configuration for the SPA (no auth)
/mcp              — MCP endpoint (JWTVerifier handles auth via JWKS)
/api/actions      — REST API for action management
/api/events       — SSE stream for real-time updates
/api/oauth/*      — OAuth provider status
/static/frontend  — bundled Svelte SPA assets
/                 — SPA shell
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Response
from fastapi.staticfiles import StaticFiles
from fastmcp.server.auth.providers.jwt import JWTVerifier
from pydantic import BaseModel
from starlette.responses import HTMLResponse, StreamingResponse

from airlock.config import Settings, build_oauth_providers
from airlock.models import (
    Action,
    ActionKey,
    ActionStatus,
    ApproveDecision,
    ConnectedOAuthStatus,
    DenyDecision,
    DisconnectedOAuthStatus,
    OAuthProviderStatus,
)
from airlock.oauth.k8s_client import K8sTokenStore
from airlock.oauth.provider import PlaidProvider, Provider
from airlock.oauth.refresh import token_refresh_loop
from airlock.oauth.routes import create_oauth_router
from airlock.predicates import load_predicate
from airlock.proxy_server import AirlockServer

logger = logging.getLogger(__name__)

_FRONTEND_DIST_DIR = Path(__file__).parent / "frontend" / "dist"

_NS_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")


class RejectBody(BaseModel):
    reason: str | None = None


def _fetch_oidc_discovery(issuer: str) -> dict[str, Any]:
    """Fetch the OpenID Connect discovery document from the issuer."""
    url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
    resp = httpx.get(url, timeout=10.0)
    resp.raise_for_status()
    result: dict[str, Any] = resp.json()
    return result


def _detect_namespace() -> str:
    """Read the in-cluster namespace, falling back to 'airlock'."""
    if _NS_PATH.exists():
        return _NS_PATH.read_text().strip()
    return "airlock"


def create_app(settings: Settings, *, include_static: bool = True) -> FastAPI:
    """Build the FastAPI app serving UI, REST API, and MCP on a single port."""
    discovery = _fetch_oidc_discovery(settings.oidc_issuer)
    predicate = load_predicate(settings.predicate_path)
    gate = AirlockServer(
        backends=settings.backends,
        db_path=settings.db_path,
        predicate=predicate,
        public_base_url=settings.public_base_url,
        default_wait_mode=settings.default_wait_mode,
        auth=JWTVerifier(jwks_uri=discovery["jwks_uri"]),
    )
    mcp_app = gate.http_app(path="/")

    oauth_providers: dict[str, Provider] = {}
    oauth_k8s_store: K8sTokenStore | None = None
    oauth_target_ns: str = ""

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal oauth_providers, oauth_k8s_store, oauth_target_ns
        oauth_providers = build_oauth_providers(settings.oauth)
        oauth_k8s_store = await K8sTokenStore.from_incluster(managed_by=settings.oauth.managed_by)
        oauth_target_ns = settings.oauth.target_namespace or _detect_namespace()

        app.include_router(create_oauth_router(oauth_providers, oauth_k8s_store, oauth_target_ns))

        task = asyncio.create_task(token_refresh_loop(oauth_providers, oauth_k8s_store, oauth_target_ns))
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="Airlock", docs_url=None, redoc_url=None, lifespan=lifespan)

    # Mount MCP sub-app for agent clients (its own lifespan manages the gate).
    app.mount("/mcp", mcp_app)

    # ── Routes ───────────────────────────────────────────────────────────────

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/auth/config")
    async def auth_config() -> dict[str, str]:
        return {
            "authority": settings.oidc_issuer,
            "client_id": settings.oidc_client_id,
            "redirect_uri": f"{settings.public_base_url}/auth/callback",
        }

    @app.get("/api/actions")
    async def list_actions(status: str | None = None, limit: int = 100, offset: int = 0) -> list[Action]:
        status_enum = ActionStatus(status) if status else None
        return await gate._req_storage.list_actions(status_enum, limit=limit, offset=offset)

    @app.get("/api/actions/{session_key}/{action_seq}")
    async def get_action(session_key: str, action_seq: int) -> Action:
        key = ActionKey(session_key=session_key, action_seq=action_seq)
        action = await gate._req_storage.get_action(key)
        if action is None:
            raise HTTPException(status_code=404, detail="Action not found")
        return action

    @app.post("/api/actions/{session_key}/{action_seq}/approve", status_code=204)
    async def approve_action(session_key: str, action_seq: int) -> Response:
        key = ActionKey(session_key=session_key, action_seq=action_seq)
        await gate.decide(key, ApproveDecision())
        return Response(status_code=204)

    @app.post("/api/actions/{session_key}/{action_seq}/reject", status_code=204)
    async def reject_action(session_key: str, action_seq: int, body: RejectBody | None = None) -> Response:
        key = ActionKey(session_key=session_key, action_seq=action_seq)
        reason = body.reason if body else None
        await gate.decide(key, DenyDecision(reason=reason))
        return Response(status_code=204)

    @app.get("/api/events")
    async def events() -> StreamingResponse:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
        gate.add_sse_listener(queue)

        async def generate():
            try:
                while True:
                    event = await queue.get()
                    yield f"data: {json.dumps(event)}\n\n"
            finally:
                gate.remove_sse_listener(queue)

        return StreamingResponse(generate(), media_type="text/event-stream")

    @app.get("/api/oauth/providers")
    async def list_oauth_providers() -> list[OAuthProviderStatus]:
        assert oauth_k8s_store is not None
        result: list[OAuthProviderStatus] = []
        for name, provider in oauth_providers.items():
            token = await oauth_k8s_store.read_token(provider.config.refresh_secret.name, oauth_target_ns)
            provider_type = "plaid" if isinstance(provider, PlaidProvider) else "oauth2"
            status = (
                ConnectedOAuthStatus(expires_at=token.expires_at, scope=token.scope)
                if token
                else DisconnectedOAuthStatus()
            )
            result.append(
                OAuthProviderStatus(
                    name=name, display_name=provider.config.display_name, provider_type=provider_type, status=status
                )
            )
        return result

    # ── Static files + SPA catch-all ─────────────────────────────────────────

    if include_static:
        _html = (_FRONTEND_DIST_DIR / "index.html").read_text()

        app.mount("/static/frontend", StaticFiles(directory=str(_FRONTEND_DIST_DIR)))

        @app.get("/{rest:path}")
        async def index(rest: str) -> HTMLResponse:
            return HTMLResponse(_html)

    return app


async def _serve() -> None:
    settings = Settings.load()
    app = create_app(settings)
    logger.info("serving on %s:%d", settings.host, settings.port)
    server = uvicorn.Server(uvicorn.Config(app, host=settings.host, port=settings.port, log_level="info"))
    await server.serve()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s", stream=sys.stderr)
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
