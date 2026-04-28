"""Study Casino backend — Y.Doc-backed multi-device sync.

The wire surface is one HTTP endpoint, plus health and the static
frontend. Clients (the Yjs `Y.Doc` running in the React PWA) sync
their local doc against the server's canonical doc using two binary
blobs encoded as base64 in a JSON envelope:

    POST /sync
      body: { state_vector_b64: str, update_b64: str }

      `state_vector_b64`  — Y.encodeStateVector(localDoc), the client's
                            knowledge of which ops it already has.
      `update_b64`        — Y.encodeStateAsUpdate(localDoc, lastServerSV),
                            the ops the client wants the server to merge.
                            May be empty ("") for a pure pull.

      → 200 { update_b64: str, state_vector_b64: str }
            on success: server merged the client's update, applied
            validators, persisted, and is returning the binary update
            the client still needs to catch up to current canonical.

      → 409 { rejection: { rule: str, message: str } }
            on validation failure: canonical is unchanged, the client
            should undo its last local transaction (Y.UndoManager) and
            surface the rule + message in a SyncIcon toast.

There is no `GET /state` and no `POST /events` — all state lives in
the Y.Doc, and the only way to mutate it is via `/sync`.

Multi-user: each authenticated user gets a separate SQLite database
(`casino-<username>.db`). When OIDC is not configured the app falls
back to a single "default" user, keeping existing tests working.
"""

import asyncio
import base64
import contextlib
import logging
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Annotated

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from x.auragon_study_casino.auth import create_oidc_router, decode_session_token, make_current_user_dep
from x.auragon_study_casino.config import Settings
from x.auragon_study_casino.store import Accepted, DocStore, Rejected

logger = logging.getLogger(__name__)

# Only allow filesystem-safe characters in usernames to prevent path traversal.
_SAFE_USERNAME = re.compile(r"^[a-zA-Z0-9._@-]{1,64}$")


class _WSManager:
    """Per-user WebSocket registry for server-push fan-out across tabs."""

    def __init__(self) -> None:
        self._connections: dict[str, set[WebSocket]] = defaultdict(set)

    def add(self, username: str, ws: WebSocket) -> None:
        self._connections[username].add(ws)

    def remove(self, username: str, ws: WebSocket) -> None:
        self._connections[username].discard(ws)

    async def push(self, username: str, message: dict, exclude: WebSocket | None = None) -> None:
        """Fan out `message` to every connected client for `username` except `exclude`."""
        for ws in list(self._connections.get(username, ())):
            if ws is exclude:
                continue
            with contextlib.suppress(Exception):
                await ws.send_json(message)


class SyncRequest(BaseModel):
    state_vector_b64: str = Field(min_length=0, max_length=4 * 1024 * 1024)
    update_b64: str = Field(min_length=0, max_length=4 * 1024 * 1024)


class SyncSuccess(BaseModel):
    update_b64: str
    state_vector_b64: str


class SyncRejection(BaseModel):
    rule: str
    message: str


class SyncRejectionEnvelope(BaseModel):
    rejection: SyncRejection


def create_app(settings: Settings) -> FastAPI:
    data_dir = settings.data_dir
    frontend_dist = settings.frontend_dist_dir or (Path(__file__).parent / "frontend" / "dist")

    # Per-user DocStore registry. Keys are sanitised usernames; stores are
    # created lazily on first request for that user.
    stores: dict[str, DocStore] = {}

    def get_store(username: str) -> DocStore:
        if username not in stores:
            if not _SAFE_USERNAME.match(username):
                raise HTTPException(status_code=400, detail=f"invalid username: {username!r}")
            stores[username] = DocStore(data_dir / f"casino-{username}.db")
        return stores[username]

    oidc = settings.oidc_config()
    current_user_dep = make_current_user_dep(oidc.session_secret if oidc else None)
    ws_manager = _WSManager()

    app = FastAPI(title="Study Casino", docs_url=None, redoc_url=None)
    app.state.current_user_dep = current_user_dep

    if oidc:
        app.include_router(
            create_oidc_router(
                issuer=oidc.issuer,
                client_id=oidc.client_id,
                client_secret=oidc.client_secret,
                session_secret=oidc.session_secret,
                public_url=oidc.public_url,
            )
        )

    @app.get("/healthz")
    def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/me")
    def me(username: Annotated[str, Depends(current_user_dep)]) -> dict[str, str]:
        return {"username": username}

    @app.websocket("/ws")
    async def websocket_sync(ws: WebSocket) -> None:
        """WebSocket sync endpoint.

        Protocol (JSON, both directions):
          Client → server: {"type":"sync","state_vector_b64":"...","update_b64":"..."}
          Server → client: {"type":"accepted","update_b64":"...","state_vector_b64":"..."}
                         | {"type":"rejected","rule":"...","message":"..."}
                         | {"type":"server_push","update_b64":"..."}  (fan-out from another tab)
                         | {"type":"error","code":N,"message":"..."}
        """
        # Auth: read the HMAC-signed session cookie from the WS upgrade request.
        if oidc is not None:
            casino_session = ws.cookies.get("casino_session")
            if not casino_session:
                await ws.close(code=4001, reason="not authenticated")
                return
            username = decode_session_token(casino_session, oidc.session_secret)
            if username is None:
                await ws.close(code=4001, reason="session invalid or expired")
                return
        else:
            username = "default"

        await ws.accept()
        store = get_store(username)
        ws_manager.add(username, ws)
        logger.info("ws connected: user=%s", username)

        # Bootstrap the client with the full canonical state immediately.
        init_update, init_sv = await asyncio.to_thread(store.snapshot_for_client, None)
        await ws.send_json(
            {
                "type": "accepted",
                "update_b64": base64.b64encode(init_update).decode("ascii"),
                "state_vector_b64": base64.b64encode(init_sv).decode("ascii"),
            }
        )

        try:
            while True:
                try:
                    data = await ws.receive_json()
                except Exception:
                    break

                if data.get("type") != "sync":
                    continue

                try:
                    client_sv = base64.b64decode(data.get("state_vector_b64") or "")
                    client_update = base64.b64decode(data.get("update_b64") or "")
                except (ValueError, TypeError) as e:
                    await ws.send_json({"type": "error", "code": 400, "message": f"invalid base64: {e}"})
                    continue

                if not client_update:
                    # Pure pull — no new data from client.
                    srv_update, srv_sv = await asyncio.to_thread(store.snapshot_for_client, client_sv)
                    await ws.send_json(
                        {
                            "type": "accepted",
                            "update_b64": base64.b64encode(srv_update).decode("ascii"),
                            "state_vector_b64": base64.b64encode(srv_sv).decode("ascii"),
                        }
                    )
                    continue

                result = await asyncio.to_thread(store.apply_client_update, client_update, client_sv)

                if isinstance(result, Rejected):
                    logger.info("ws sync rejected: user=%s rule=%s", username, result.rule)
                    await ws.send_json({"type": "rejected", "rule": result.rule, "message": result.message})
                    continue

                assert isinstance(result, Accepted)
                logger.info("ws sync accepted: user=%s", username)

                await ws.send_json(
                    {
                        "type": "accepted",
                        "update_b64": base64.b64encode(result.server_update).decode("ascii"),
                        "state_vector_b64": base64.b64encode(result.server_state_vector).decode("ascii"),
                    }
                )

                # Fan the full canonical state out to every other connected tab for this user.
                full_update = await asyncio.to_thread(store.get_update_for_client, None)
                await ws_manager.push(
                    username,
                    {"type": "server_push", "update_b64": base64.b64encode(full_update).decode("ascii")},
                    exclude=ws,
                )

        except WebSocketDisconnect:
            pass
        finally:
            ws_manager.remove(username, ws)
            logger.info("ws disconnected: user=%s", username)

    @app.post("/sync", response_model=SyncSuccess)
    def sync(body: SyncRequest, username: Annotated[str, Depends(current_user_dep)]) -> SyncSuccess | JSONResponse:
        store = get_store(username)

        try:
            client_sv = base64.b64decode(body.state_vector_b64)
            client_update = base64.b64decode(body.update_b64)
        except (ValueError, TypeError) as e:
            raise HTTPException(status_code=400, detail=f"invalid base64: {e}") from e

        if not client_update:
            server_update, server_sv = store.snapshot_for_client(client_sv)
            return SyncSuccess(
                update_b64=base64.b64encode(server_update).decode("ascii"),
                state_vector_b64=base64.b64encode(server_sv).decode("ascii"),
            )

        result = store.apply_client_update(client_update, client_sv)
        if isinstance(result, Rejected):
            logger.info("sync rejected: user=%s rule=%s", username, result.rule)
            envelope = SyncRejectionEnvelope(rejection=SyncRejection(rule=result.rule, message=result.message))
            return JSONResponse(status_code=409, content=envelope.model_dump())

        assert isinstance(result, Accepted)
        logger.info("sync accepted: user=%s", username)
        return SyncSuccess(
            update_b64=base64.b64encode(result.server_update).decode("ascii"),
            state_vector_b64=base64.b64encode(result.server_state_vector).decode("ascii"),
        )

    if frontend_dist.exists():
        app.mount("/", StaticFiles(directory=str(frontend_dist), html=True), name="frontend")
    else:
        logger.warning("frontend dist dir %s not found — serving API only", frontend_dist)

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s", stream=sys.stderr)
    settings = Settings()
    logger.info("study casino listening on %s:%d, data_dir=%s", settings.host, settings.port, settings.data_dir)
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
