"""Owner-authenticated read API and browser for archived workstation sessions."""

from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.engine import Engine
from starlette import status

from util.bazel.runfiles import find_path
from util.oidc_login import LoginConfig, install_login
from x.ai_session_archive import queries
from x.ai_session_archive.database import make_session_factory
from x.ai_session_archive.models import EventPage, Harness, Machine, SessionPage, SessionSummary, SessionVersion
from x.ai_session_archive.web_settings import WebSettings

_FRONTEND_INDEX = "_main/devinfra/session_viewer/frontend/dist/index.html"


class Machines(BaseModel):
    data: list[Machine]


class Versions(BaseModel):
    data: list[SessionVersion]


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def create_app(*, engine: Engine, settings: WebSettings, frontend_dir: Path | None = None) -> FastAPI:
    app = FastAPI(title="Workstation session archive")
    factory = make_session_factory(engine)
    api = APIRouter(prefix="/api", dependencies=[Depends(_no_store)])

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @api.get("/machines")
    def machines() -> Machines:
        with factory() as db:
            return Machines(data=queries.list_machines(db))

    @api.get("/sessions")
    def sessions(
        machine_id: str | None = None,
        harness: Harness | None = None,
        parent_session_id: UUID | None = None,
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> SessionPage:
        with factory() as db:
            try:
                return queries.list_sessions(
                    db,
                    machine_id=machine_id,
                    harness=harness,
                    parent_session_id=parent_session_id,
                    cursor=cursor,
                    limit=limit,
                )
            except ValueError as invalid:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid session cursor") from invalid

    @api.get("/sessions/{session_id}")
    def session_detail(session_id: UUID) -> SessionSummary:
        with factory() as db:
            result = queries.get_session(db, session_id=session_id)
            if result is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
            return result

    @api.get("/sessions/{session_id}/versions")
    def versions(session_id: UUID) -> Versions:
        with factory() as db:
            if queries.get_session(db, session_id=session_id) is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
            return Versions(data=queries.list_versions(db, session_id=session_id))

    @api.get("/sessions/{session_id}/versions/{version_id}/events")
    def events(
        session_id: UUID,
        version_id: UUID,
        cursor: Annotated[int | None, Query(ge=0)] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> EventPage:
        with factory() as db:
            page = queries.list_events(db, session_id=session_id, version_id=version_id, cursor=cursor, limit=limit)
            if page is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Session version not found")
            return page

    app.include_router(api)
    if frontend_dir is None:
        index = find_path(_FRONTEND_INDEX)
        if index is not None:
            frontend_dir = index.parent
    if frontend_dir is not None:
        index = frontend_dir / "index.html"

        @app.get("/", include_in_schema=False)
        def home() -> FileResponse:
            return FileResponse(index, headers={"Cache-Control": "no-store"})

        app.mount("/", StaticFiles(directory=frontend_dir), name="frontend")
    install_login(
        app,
        LoginConfig(
            issuer=settings.oidc_issuer,
            client_id=settings.oidc_client_id,
            client_secret=settings.oidc_client_secret,
            session_secret=settings.oidc_session_secret,
            session_seconds=settings.oidc_session_seconds,
            public_base_url=settings.public_base_url,
            cookie_name="ai-session-archive",
            public_paths={"/healthz"},
            signed_out_text="You are signed out.",
            session_created_log=None,
            allowed_subject=settings.oidc_allowed_subject,
        ),
    )
    return app
