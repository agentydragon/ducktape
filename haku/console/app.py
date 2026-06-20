"""FastAPI app for the Haku console: JSON API + same-origin React SPA.

The dashboard is a React single-page app (static bundle) served same-origin with a
JSON API under ``/api``. The write endpoints are **generic**: clicking an action
records ``clicks/<item>/<action>`` and un-clicking removes it — the backend never
interprets what an action *means* (snooze, reject, research…); Haku reduces the
clicks overlay on its next run. Free-form feedback (global, or tagged to an item)
appends to ``intake/``.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from haku.console.config import Settings
from haku.console.git_state import GitState
from haku.console.models import Click, DashboardResponse, FeedbackRequest

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

    # Generic action recording: the backend never interprets an action's meaning — it
    # only records (POST) or retracts (DELETE) the click. Haku reads the clicks/ overlay
    # on its next run and carries out each action's intent.
    @app.post("/api/items/{item_id}/actions/{action_id}")
    async def click(item_id: str, action_id: str) -> dict[str, str]:
        async with git_state.lock:
            await asyncio.to_thread(git_state.set_click, item_id, action_id)
        return {"status": "clicked"}

    @app.delete("/api/items/{item_id}/actions/{action_id}")
    async def unclick(item_id: str, action_id: str) -> dict[str, str]:
        async with git_state.lock:
            await asyncio.to_thread(git_state.clear_click, item_id, action_id)
        return {"status": "cleared"}

    @app.post("/api/feedback")
    async def feedback(req: FeedbackRequest) -> dict[str, str]:
        async with git_state.lock:
            await asyncio.to_thread(git_state.write_feedback, req.text, req.item_id)
        return {"status": "ok"}

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
