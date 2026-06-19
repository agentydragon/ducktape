"""FastAPI app for the Haku dashboard "arm".

Renders the dashboard read-only from a haku-state clone (refreshed by a background
pull loop) and records operator actions as git commits. The write endpoints are
**generic**: clicking an action records ``clicks/<item>/<action>`` and un-clicking
removes it — the backend never interprets what an action *means* (snooze, reject,
research…); Haku reduces the clicks overlay on its next run. Free-form feedback
appends to ``intake/``.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
from typing import Annotated

import uvicorn
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from haku.arm import renderer, templates_loader
from haku.arm.config import Settings
from haku.arm.git_state import GitState

logger = logging.getLogger(__name__)


def _see_home() -> RedirectResponse:
    """POST-redirect-GET back to the dashboard after a write."""
    return RedirectResponse("/", status_code=303)


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

    app = FastAPI(title="Haku dashboard arm", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        async with git_state.lock:
            items = await asyncio.to_thread(git_state.read_items)
            clicks = await asyncio.to_thread(git_state.read_clicks)
            css = await asyncio.to_thread(templates_loader.load_css, settings.clone_dir)
            template = await asyncio.to_thread(templates_loader.load_page_template, settings.clone_dir)
        scan = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M UTC")
        page = renderer.render_page(items, scan_time=scan, page_template=template, css=css, clicks=clicks)
        return HTMLResponse(page)

    # Generic action recording: the backend never interprets an action's meaning —
    # it only records (POST) or retracts (DELETE / …/unclick) the click. Haku reads
    # the clicks/ overlay on its next run and carries out each action's intent.
    @app.post("/items/{item_id}/actions/{action_id}")
    async def click(item_id: str, action_id: str) -> RedirectResponse:
        async with git_state.lock:
            await asyncio.to_thread(git_state.set_click, item_id, action_id)
        return _see_home()

    # Plain-HTML forms can only POST, so the toggle's un-click posts here.
    @app.post("/items/{item_id}/actions/{action_id}/unclick")
    async def unclick(item_id: str, action_id: str) -> RedirectResponse:
        async with git_state.lock:
            await asyncio.to_thread(git_state.clear_click, item_id, action_id)
        return _see_home()

    @app.delete("/items/{item_id}/actions/{action_id}")
    async def delete_click(item_id: str, action_id: str) -> dict[str, str]:
        async with git_state.lock:
            await asyncio.to_thread(git_state.clear_click, item_id, action_id)
        return {"status": "cleared"}

    @app.post("/feedback")
    async def feedback(text: Annotated[str, Form()]) -> RedirectResponse:
        async with git_state.lock:
            await asyncio.to_thread(git_state.write_feedback, text)
        return _see_home()

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
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
