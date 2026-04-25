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
                            May be empty (\"\") for a pure pull.

      → 200 { update_b64: str, state_vector_b64: str }
            on success: server merged the client's update, applied
            validators, persisted, and is returning the binary update
            the client still needs to catch up to current canonical.

      → 409 { rejection: { rule: str, message: str } }
            on validation failure: canonical is unchanged, the client
            should undo its last local transaction (Y.UndoManager) and
            surface the rule + message in a SyncBanner toast.

There is no `GET /state` and no `POST /events` — all state lives in
the Y.Doc, and the only way to mutate it is via `/sync`. Sits behind
an Authentik proxy outpost; backend reads `X-Authentik-Username`
purely for logging.
"""

from __future__ import annotations

import base64
import logging
import sys
from pathlib import Path
from typing import Annotated

import uvicorn
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from x.auragon_study_casino.config import Settings
from x.auragon_study_casino.store import Accepted, DocStore, Rejected

logger = logging.getLogger(__name__)


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
    store = DocStore(settings.data_dir / "casino.db")
    frontend_dist = settings.frontend_dist_dir or (Path(__file__).parent / "frontend" / "dist")

    app = FastAPI(title="Study Casino", docs_url=None, redoc_url=None)

    @app.get("/healthz")
    def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/sync", response_model=SyncSuccess)
    def sync(
        body: SyncRequest, x_authentik_username: Annotated[str | None, Header()] = None
    ) -> SyncSuccess | JSONResponse:
        try:
            client_sv = base64.b64decode(body.state_vector_b64)
            client_update = base64.b64decode(body.update_b64)
        except (ValueError, TypeError) as e:
            raise HTTPException(status_code=400, detail=f"invalid base64: {e}") from e

        if not client_update:
            # Pure pull: the client just wants to know what it is missing.
            server_update = store.get_update_for_client(client_sv)
            return SyncSuccess(
                update_b64=base64.b64encode(server_update).decode("ascii"),
                state_vector_b64=base64.b64encode(store.get_server_state_vector()).decode("ascii"),
            )

        result = store.apply_client_update(client_update, client_sv)
        if isinstance(result, Rejected):
            logger.info("sync rejected: user=%s rule=%s", x_authentik_username, result.rule)
            envelope = SyncRejectionEnvelope(rejection=SyncRejection(rule=result.rule, message=result.message))
            return JSONResponse(status_code=409, content=envelope.model_dump())

        assert isinstance(result, Accepted)
        logger.info("sync accepted: user=%s", x_authentik_username)
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
