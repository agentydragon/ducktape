"""Study Casino backend.

Serves a React PWA from `frontend/dist/` and exposes a GET/PUT `/state` endpoint
for cross-device sync of the app's state blob. Sits behind an Authentik proxy
outpost (forward-auth mode); does not re-validate the JWT. The outpost's
`X-Authentik-Username` header is logged for observability only — since this is
a single-user app there is no per-user scoping.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Annotated

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles

from x.auragon_study_casino.config import Settings
from x.auragon_study_casino.storage import StateStore

logger = logging.getLogger(__name__)


def create_app(settings: Settings) -> FastAPI:
    store = StateStore(settings.data_dir / "state.db")
    frontend_dist = settings.frontend_dist_dir or (Path(__file__).parent / "frontend" / "dist")

    app = FastAPI(title="Study Casino", docs_url=None, redoc_url=None)

    @app.get("/healthz")
    def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/state")
    def get_state(request: Request, x_authentik_username: Annotated[str | None, Header()] = None) -> Response:
        record = store.load()
        if record is None:
            # First run: 200 with empty object so the frontend just starts fresh
            # without needing to special-case 404.
            return Response(
                content=b"{}", media_type="application/json", headers={"ETag": '"empty"', "Cache-Control": "no-store"}
            )
        return Response(
            content=record.blob,
            media_type="application/json",
            headers={"ETag": record.etag, "Cache-Control": "no-store"},
        )

    @app.put("/state")
    async def put_state(
        request: Request,
        x_authentik_username: Annotated[str | None, Header()] = None,
        if_match: Annotated[str | None, Header()] = None,
    ) -> Response:
        body = await request.body()
        if not body:
            raise HTTPException(status_code=400, detail="Empty body")
        current = store.load()
        current_etag = current.etag if current else '"empty"'
        if if_match is not None and if_match != current_etag:
            raise HTTPException(status_code=412, detail="ETag mismatch")
        record = store.save(body)
        logger.info("state updated by user=%s size=%d", x_authentik_username, len(body))
        return Response(content=b"", status_code=204, headers={"ETag": record.etag})

    # Static frontend is mounted last so /state and /healthz take precedence.
    # The PWA is a single-page app; unknown paths fall through to index.html so
    # deep links like /settings still load the shell.
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
