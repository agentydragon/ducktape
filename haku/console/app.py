"""FastAPI app for the Haku console: JSON API + same-origin React SPA.

The dashboard is a React single-page app (static bundle) served same-origin with a
JSON API under ``/api``. Writes are split into tiers (see `haku/PLAN.md` → _The
agent-authored console_): the **trace tier** (`haku.console.trace`) only records
operator-expressed intent into haku-state — clicks (the overlay Haku reduces) and
feedback — and is the low-privilege surface safe for agent-authored UI. The
high-privilege **capability tier** (`haku.console.capabilities`) uses console-only
secrets and acts on the world (launching the routine); it is CSRF-gated and audited.
``app.py`` wires both routers, configures CSRF, and serves the read endpoints + SPA.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
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
from haku.console.models import Click, DashboardResponse

logger = logging.getLogger(__name__)


async def _pull_loop(git_state: GitState, interval_s: float) -> None:
    """Periodically reconcile the clone so the dashboard reflects Haku's runs."""
    while True:
        await asyncio.sleep(interval_s)
        try:
            async with git_state.lock:
                await asyncio.to_thread(git_state.reconcile)
        except Exception:
            logger.warning("background pull failed", exc_info=True)


def create_app(settings: Settings, *, git_state: GitState) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        await asyncio.to_thread(git_state.clone_or_open)
        pull = asyncio.create_task(_pull_loop(git_state, settings.pull_interval_s))
        try:
            yield
        finally:
            pull.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pull

    app = FastAPI(title="Haku console", lifespan=lifespan)
    # Routers read git_state / settings off app.state (see haku.console.{trace,capabilities}).
    app.state.git_state = git_state
    app.state.settings = settings

    # Content-Security-Policy: let the dashboard frame Haku's own UI origin (the
    # sandboxed cross-origin iframe) and nothing else, and forbid the console itself
    # from being framed. Only frame-* is set, so the SPA's own scripts/styles are
    # unaffected. See haku/console/plans/free_form_ui_iframe.md.
    frame_src = f"'self' {settings.haku_ui_url}" if settings.haku_ui_url else "'none'"
    csp = f"frame-src {frame_src}; frame-ancestors 'none'"

    @app.middleware("http")
    async def _csp_headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = csp
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

    @app.get("/api/dashboard")
    async def dashboard() -> DashboardResponse:
        """Items (with their currently-clicked action ids) and the last scan time."""
        async with git_state.lock:
            items = await asyncio.to_thread(git_state.read_items)
            clicks = await asyncio.to_thread(git_state.read_clicks)
        scan_time = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M UTC")
        clicked = [Click(item_id=item_id, action_id=action_id) for item_id, action_id in sorted(clicks)]
        launch = settings.launch_routine
        return DashboardResponse(
            scan_time=scan_time,
            items=items,
            clicks=clicked,
            launch_routine_url=launch.page_url if launch else None,
            haku_ui_url=settings.haku_ui_url,
        )

    app.include_router(trace.router)
    app.include_router(capabilities.router)

    # The built React SPA is served same-origin for everything else. Mounted last so the
    # API routes above take precedence; left unmounted in tests (static_dir unset).
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
