"""FastAPI app for the Haku dashboard "arm".

Milestone A: renders the dashboard read-only from a haku-state clone, refreshed by
a background pull loop. Write endpoints (operator actions → git commits) land in
later milestones.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from haku.arm import renderer, templates_loader
from haku.arm.config import Settings
from haku.arm.git_state import GitState

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

    app = FastAPI(title="Haku dashboard arm", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        async with git_state.lock:
            items = await asyncio.to_thread(git_state.read_items)
            css = await asyncio.to_thread(templates_loader.load_css, settings.clone_dir)
            template = await asyncio.to_thread(templates_loader.load_page_template, settings.clone_dir)
        scan = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M UTC")
        return HTMLResponse(renderer.render_page(items, scan_time=scan, page_template=template, css=css))

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
