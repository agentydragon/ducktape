"""FastAPI app for the Haku console JSON API.

The console is a thin shell: the capability tier (launch-routine) + a generic
"Note to Haku" trace box + the Free-form UI iframe. Item rendering has moved to
``haku/state_template/ui/`` — Haku's own UI, embedded via iframe.

Writes are split into tiers (see ``haku/PLAN.md`` → _The agent-authored console_):
the **trace tier** (``haku.console.trace``) records opaque operator text into
haku-state and is the low-privilege surface safe for agent-authored UI. The
high-privilege **capability tier** (``haku.console.capabilities``) uses console-only
secrets and acts on the world (launching the routine); it is CSRF-gated and audited.
``app.py`` wires both routers, configures CSRF, and serves the config endpoint. It
can also mount the built SPA when ``static_dir`` is explicitly configured for a
direct local/dev fallback.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from collections.abc import Awaitable, Callable

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi_csrf_protect import CsrfProtect
from fastapi_csrf_protect.exceptions import CsrfProtectError

from haku.console import capabilities, trace
from haku.console.config import Settings
from haku.console.git_state import GitState
from haku.console.models import ConfigResponse

logger = logging.getLogger(__name__)

APP_SHELL_CACHE_CONTROL = "no-cache, max-age=0, must-revalidate"
IMMUTABLE_ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"
NO_STORE_CACHE_CONTROL = "no-store"
REFERRER_POLICY = "no-referrer"


def _cache_control_for_path(path: str) -> str:
    if path.startswith("/assets/"):
        return IMMUTABLE_ASSET_CACHE_CONTROL
    if path.startswith("/api/") or path == "/healthz":
        return NO_STORE_CACHE_CONTROL
    return APP_SHELL_CACHE_CONTROL


def create_app(settings: Settings, *, git_state: GitState) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        # Clone is still needed for trace writes (append_trace → commit_push).
        await asyncio.to_thread(git_state.clone_or_open)
        yield

    app = FastAPI(title="Haku console", lifespan=lifespan)
    # Routers read git_state / settings off app.state (see haku.console.{trace,capabilities}).
    app.state.git_state = git_state
    app.state.settings = settings

    # Content-Security-Policy: let the dashboard frame Haku's own UI origin (the
    # sandboxed cross-origin iframe) and Authentik's origin for the SSO redirect,
    # and forbid the console itself from being framed. Only frame-* is set, so the
    # SPA's own scripts/styles are unaffected. See haku/console/plans/free_form_ui_iframe.md.
    frame_src = f"'self' {settings.haku_ui_url} {settings.auth_origin}" if settings.haku_ui_url else "'none'"
    csp = f"frame-src {frame_src}; frame-ancestors 'none'"

    @app.middleware("http")
    async def _security_headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = csp
        response.headers["Cache-Control"] = _cache_control_for_path(request.url.path)
        response.headers["Referrer-Policy"] = REFERRER_POLICY
        return response

    # CSRF for the capability tier: a header-located double-submit token (the SPA
    # echoes the token from GET /api/capabilities/csrf in X-CSRF-Token). Use the
    # configured secret, else an ephemeral one (fine for the single-replica console
    # — a restart just makes the SPA refetch its token).
    csrf_secret = settings.csrf_secret.get_secret_value() if settings.csrf_secret else secrets.token_urlsafe(32)
    CsrfProtect.load_config(lambda: [("secret_key", csrf_secret), ("token_location", "header")])

    @app.exception_handler(CsrfProtectError)
    async def _csrf_error(request: Request, exc: CsrfProtectError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/config")
    async def config() -> ConfigResponse:
        """Static config for the SPA: launch-routine URL and Haku UI URL."""
        launch = settings.launch_routine
        return ConfigResponse(launch_routine_url=launch.page_url if launch else None, haku_ui_url=settings.haku_ui_url)

    app.include_router(trace.router)
    app.include_router(capabilities.router)

    # Optional direct local/dev fallback. Production serves the SPA from the
    # haku-console-static nginx image and leaves static_dir unset on this process.
    # Mounted last so the API routes above take precedence.
    if settings.static_dir is not None and settings.static_dir.is_dir():
        app.mount("/", StaticFiles(directory=settings.static_dir, html=True), name="spa")

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    settings = Settings()
    git_state = GitState(
        repo_url=settings.git_repo_url,
        username=settings.git_username,
        password=settings.git_password.get_secret_value(),
        clone_dir=settings.clone_dir,
        branch=settings.branch,
    )
    app = create_app(settings, git_state=git_state)
    # host/port are fixed, not env-driven: under the HAKU_CONSOLE_ prefix a `port`
    # setting would read the kubelet's HAKU_CONSOLE_PORT service-link var (a URL),
    # not an int. The deployment also disables service links (enableServiceLinks: false).
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")


if __name__ == "__main__":
    main()
