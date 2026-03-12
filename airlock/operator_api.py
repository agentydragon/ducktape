"""REST API for the operator frontend.

Replaces the former MCP-based operator tools (approve_action, reject_action,
list_actions) with standard HTTP endpoints. The operator SPA authenticates via
OIDC (Authorization Code + PKCE) and sends the JWT as a Bearer token.

JWT validation uses fastmcp's JWTVerifier (same JWKS infra as the MCP auth path)
but checks for the ``decide`` scope instead of ``propose``.

SSE endpoint provides live action updates to the frontend.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.providers.jwt import JWTVerifier
from pydantic import BaseModel

from airlock.coordinator import ActionCoordinator, ActionCreatedEvent, CoordinatorEvent
from airlock.models import Action, ActionKey, ActionStatus

logger = logging.getLogger(__name__)

DECIDE_SCOPE = "decide"


class RejectBody(BaseModel):
    reason: str | None = None


class _OperatorAuth:
    """Validates operator JWTs using fastmcp's JWTVerifier.

    Lazily discovers the JWKS URI from the OIDC provider on first use.
    """

    def __init__(self, oidc_issuer: str) -> None:
        self._issuer = oidc_issuer
        self._verifier: JWTVerifier | None = None

    async def _ensure_verifier(self) -> JWTVerifier:
        if self._verifier is not None:
            return self._verifier
        config_url = f"{self._issuer.rstrip('/')}/.well-known/openid-configuration"
        async with httpx.AsyncClient() as client:
            resp = await client.get(config_url)
            resp.raise_for_status()
            jwks_uri = resp.json()["jwks_uri"]
        self._verifier = JWTVerifier(jwks_uri=jwks_uri, issuer=self._issuer, required_scopes=[DECIDE_SCOPE])
        return self._verifier

    async def validate(self, token: str) -> dict[str, Any]:
        """Validate a JWT and return its claims. Raises HTTPException on failure."""
        verifier = await self._ensure_verifier()
        access_token = await verifier.verify_token(token)
        if not isinstance(access_token, AccessToken):
            raise HTTPException(status_code=401, detail="Invalid token or missing required scope")
        return access_token.claims


def _extract_bearer_token(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    return auth.removeprefix("Bearer ")


def create_operator_api(*, coordinator: ActionCoordinator, oidc_issuer: str) -> FastAPI:
    """Build the FastAPI sub-application for operator REST endpoints."""
    operator_auth = _OperatorAuth(oidc_issuer)

    async def require_operator(request: Request) -> dict[str, Any]:
        token = _extract_bearer_token(request)
        return await operator_auth.validate(token)

    app = FastAPI(title="Airlock Operator API", version="1.0.0", dependencies=[Depends(require_operator)])

    # ── SSE event distribution (local to operator API) ────────────────────

    sse_subscribers: set[asyncio.Queue[dict[str, object]]] = set()

    async def _on_coordinator_event(event: CoordinatorEvent) -> None:
        event_type = "action_created" if isinstance(event, ActionCreatedEvent) else "action_updated"
        sse_event: dict[str, object] = {"type": event_type, "action": json.loads(event.action.model_dump_json())}
        for queue in sse_subscribers:
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(sse_event)

    coordinator.add_listener(_on_coordinator_event)

    # ── REST endpoints ───────────────────────────────────────────────────

    @app.get("/actions", response_model=list[Action])
    async def list_actions(
        status: ActionStatus | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ) -> list[Action]:
        return await coordinator.list_actions(status, limit=limit, offset=offset)

    @app.get("/actions/{session_key}/{action_seq}", response_model=Action)
    async def get_action(session_key: str, action_seq: int) -> Action:
        key = ActionKey(session_key=session_key, action_seq=action_seq)
        action = await coordinator.get_action(key)
        if action is None:
            raise HTTPException(status_code=404, detail="Action not found")
        return action

    @app.post("/actions/{session_key}/{action_seq}/approve", status_code=204)
    async def approve_action(session_key: str, action_seq: int) -> None:
        key = ActionKey(session_key=session_key, action_seq=action_seq)
        try:
            await coordinator.approve_action(key)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e

    @app.post("/actions/{session_key}/{action_seq}/reject", status_code=204)
    async def reject_action(session_key: str, action_seq: int, body: RejectBody) -> None:
        key = ActionKey(session_key=session_key, action_seq=action_seq)
        try:
            await coordinator.reject_action(key, reason=body.reason)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e

    @app.get("/events")
    async def sse_events(request: Request) -> StreamingResponse:
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        sse_subscribers.add(queue)

        async def event_stream():
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=30.0)
                        yield f"data: {json.dumps(event)}\n\n"
                    except TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                sse_subscribers.discard(queue)

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    return app
