"""The page that pairs the sync and shows how it is doing: a small API over `SyncSupervisor` and the SPA
that drives it (`frontend/`), behind the Authentik login of `util/oidc_login.py`."""

import logging
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette import status

from devinfra.claude.session_export.settings import ServeSettings
from devinfra.claude.session_export.supervisor import SyncStatus, SyncSupervisor
from util.bazel.runfiles import find_path
from util.oidc_login import LoginConfig, install_login

logger = logging.getLogger(__name__)

_FRONTEND_INDEX = "_main/devinfra/claude/session_export/frontend/dist/index.html"


class PairingStart(BaseModel):
    authorization_url: str = Field(description="Open it in a browser signed in to the Claude account and approve.")


class PairingFinish(BaseModel):
    redirect_url: str = Field(
        description="The address the browser was sent to after approval: it fails to load, and carries the code."
    )


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _api(supervisor: SyncSupervisor) -> APIRouter:
    router = APIRouter(prefix="/api", dependencies=[Depends(_no_store)])

    @router.get("/status")
    async def get_status() -> SyncStatus:
        return await supervisor.status()

    @router.post("/pairing")
    async def start_pairing() -> PairingStart:
        return PairingStart(authorization_url=supervisor.start_pairing())

    @router.post("/pairing/complete")
    async def finish_pairing(body: PairingFinish) -> SyncStatus:
        try:
            await supervisor.finish_pairing(body.redirect_url)
        except ValueError as refused:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(refused)) from refused
        except httpx.HTTPStatusError as refused:
            logger.exception("Anthropic refused the authorization code")
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                f"Anthropic refused the authorization code ({refused.response.status_code}); start pairing again.",
            ) from refused
        except httpx.TransportError as unreachable:
            logger.exception("could not reach Anthropic to redeem the authorization code")
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY, "Could not reach Anthropic; start pairing again."
            ) from unreachable
        return await supervisor.status()

    @router.post("/sync", status_code=status.HTTP_202_ACCEPTED)
    async def sync_now() -> None:
        supervisor.sync_now()

    return router


def _login_config(settings: ServeSettings) -> LoginConfig:
    return LoginConfig(
        issuer=settings.oidc_issuer,
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
        session_secret=settings.oidc_session_secret,
        session_seconds=settings.oidc_session_seconds,
        public_base_url=settings.public_base_url,
        cookie_name="claude-session-sync",
        public_paths={"/healthz"},
        signed_out_text="You are signed out.",
        session_created_log=None,
        allowed_subject=settings.oidc_allowed_subject,
    )


def create_app(*, supervisor: SyncSupervisor, settings: ServeSettings, frontend_dir: Path | None = None) -> FastAPI:
    app = FastAPI(title="claude-session-sync", version="1")

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(_api(supervisor))
    install_login(app, _login_config(settings))

    if frontend_dir is None:
        # Only the server binary packages the SPA. The library is also imported without it: by its own tests,
        # and by the OpenAPI exporter the frontend's types come from, which cannot depend on the bundle.
        index = find_path(_FRONTEND_INDEX)
        if index is None:
            logger.warning("the frontend bundle is not present in runfiles")
        else:
            frontend_dir = index.parent
    if frontend_dir is not None:
        app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")
    return app
