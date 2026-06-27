"""FastAPI app for Haku's own UI service: JSON API + same-origin React SPA.

This is **Haku-owned** starter code (ported from ``haku/console``): it runs in
``haku-sandbox``, behind the operator-owned Authentik outpost, embedded in the
trusted console's "Free-form UI" iframe. It reads ``items/`` + ``clicks/`` from a
haku-state clone and serves the SPA + a JSON API, and writes ``clicks/`` / ``intake/``
back to haku-state on operator action (the conventions Haku reduces on its next run).

Unlike the trusted console, there is **no capability tier** here — only the
low-privilege trace surface (clicks + feedback into haku-state, which Haku already
owns). The privileged launch-routine capability stays in the console.

**Operator authentication.** The app is only reachable through the Authentik outpost,
which injects ``X-authentik-username``. We read it to know who acted (logged on every
write). The header is only trustworthy once an ingress NetworkPolicy restricts the app
to the outpost (so a sibling haku-sandbox pod can't spoof it) — see the Phase 3
hardening note in the plan. Until then, treat the header as advisory.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
from collections.abc import Awaitable, Callable
from typing import Annotated

import uvicorn
from config import Settings
from fastapi import FastAPI, Header, Request, Response
from fastapi.staticfiles import StaticFiles
from git_state import GitState
from models import Click, DashboardResponse, FeedbackRequest

logger = logging.getLogger(__name__)

# The Authentik outpost injects X-authentik-username (advisory until the ingress
# NetworkPolicy lands — see the module docstring). None when called outside the outpost.
Operator = Annotated[str | None, Header(alias="X-authentik-username")]


async def _pull_loop(git_state: GitState, interval_s: float) -> None:
    """Periodically reconcile the clone so the UI reflects Haku's runs."""
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

    app = FastAPI(title="Haku UI", lifespan=lifespan)

    # This UI is itself framed by the console; forbid it from being framed by anyone
    # else (the console's own CSP frame-src already whitelists this origin).
    @app.middleware("http")
    async def _frame_headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = "frame-ancestors https://haku.allegedly.works"
        return response

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
        return DashboardResponse(scan_time=scan_time, items=items, clicks=clicked)

    # --- trace tier: operator-expressed intent recorded into haku-state ---------
    # Idempotent set → PUT; un-click → DELETE; feedback → POST. Haku reduces these.

    @app.put("/api/trace/items/{item_id}/actions/{action_id}")
    async def set_click(item_id: str, action_id: str, operator: Operator = None) -> dict[str, str]:
        logger.info("click %s on %s by %s", action_id, item_id, operator or "<unknown>")
        async with git_state.lock:
            await asyncio.to_thread(git_state.set_click, item_id, action_id)
        return {"status": "clicked"}

    @app.delete("/api/trace/items/{item_id}/actions/{action_id}")
    async def clear_click(item_id: str, action_id: str, operator: Operator = None) -> dict[str, str]:
        logger.info("unclick %s on %s by %s", action_id, item_id, operator or "<unknown>")
        async with git_state.lock:
            await asyncio.to_thread(git_state.clear_click, item_id, action_id)
        return {"status": "cleared"}

    @app.post("/api/trace/feedback")
    async def feedback(req: FeedbackRequest, operator: Operator = None) -> dict[str, str]:
        logger.info("feedback (item=%s) by %s", req.item_id or "<global>", operator or "<unknown>")
        async with git_state.lock:
            await asyncio.to_thread(git_state.write_feedback, req.text, req.item_id)
        return {"status": "ok"}

    # The built React SPA is served same-origin for everything else. Mounted last so the
    # API routes above take precedence; left unmounted when static_dir is unset.
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
    # host/port fixed, not env-driven (the Deployment disables service links).
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")


if __name__ == "__main__":
    main()
