"""FastAPI app for Haku's own UI service: JSON API + same-origin React SPA.

This is **Haku-owned** starter code (ported from ``haku/console``): it runs in
``haku-sandbox``, behind the operator-owned Authentik outpost, embedded in the
trusted console's "Free-form UI" iframe. It reads ``items/`` + ``clicks/`` from
haku-state and writes ``clicks/`` / ``intake/`` back on operator action (the
conventions Haku reduces on its next run) — all through the **Forgejo API** (see
``forgejo.py``), no local clone.

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

import contextlib
import datetime as dt
import logging
from collections.abc import Awaitable, Callable
from typing import Annotated

import uvicorn
from config import Settings
from fastapi import Depends, FastAPI, Header, Request, Response
from fastapi.staticfiles import StaticFiles
from forgejo import Forgejo
from models import Click, DashboardResponse, FeedbackRequest

logger = logging.getLogger(__name__)

# The Authentik outpost injects X-authentik-username (advisory until the ingress
# NetworkPolicy lands — see the module docstring). None when called outside the outpost.
Operator = Annotated[str | None, Header(alias="X-authentik-username")]


def _forgejo(request: Request) -> Forgejo:
    return request.app.state.forgejo


ForgejoDep = Annotated[Forgejo, Depends(_forgejo)]


def create_app(settings: Settings) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        async with Forgejo(
            api_url=settings.forgejo_api_url,
            username=settings.git_username,
            password=settings.git_password.get_secret_value(),
            branch=settings.branch,
        ) as forgejo:
            app.state.forgejo = forgejo
            yield

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
    async def dashboard(forgejo: ForgejoDep) -> DashboardResponse:
        """Items (with their currently-clicked action ids) and the last scan time."""
        items = await forgejo.read_items()
        clicks = await forgejo.read_clicks()
        scan_time = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M UTC")
        clicked = [Click(item_id=item_id, action_id=action_id) for item_id, action_id in sorted(clicks)]
        return DashboardResponse(scan_time=scan_time, items=items, clicks=clicked)

    # --- trace tier: operator-expressed intent recorded into haku-state ---------
    # Idempotent set → PUT; un-click → DELETE; feedback → POST. Haku reduces these.

    @app.put("/api/trace/items/{item_id}/actions/{action_id}")
    async def set_click(item_id: str, action_id: str, forgejo: ForgejoDep, operator: Operator = None) -> dict[str, str]:
        logger.info("click %s on %s by %s", action_id, item_id, operator or "<unknown>")
        await forgejo.set_click(item_id, action_id)
        return {"status": "clicked"}

    @app.delete("/api/trace/items/{item_id}/actions/{action_id}")
    async def clear_click(
        item_id: str, action_id: str, forgejo: ForgejoDep, operator: Operator = None
    ) -> dict[str, str]:
        logger.info("unclick %s on %s by %s", action_id, item_id, operator or "<unknown>")
        await forgejo.clear_click(item_id, action_id)
        return {"status": "cleared"}

    @app.post("/api/trace/feedback")
    async def feedback(req: FeedbackRequest, forgejo: ForgejoDep, operator: Operator = None) -> dict[str, str]:
        logger.info("feedback (item=%s) by %s", req.item_id or "<global>", operator or "<unknown>")
        await forgejo.write_feedback(req.text, req.item_id)
        return {"status": "ok"}

    # The built React SPA is served same-origin for everything else. Mounted last so the
    # API routes above take precedence; left unmounted when static_dir is unset.
    if settings.static_dir is not None and settings.static_dir.is_dir():
        app.mount("/", StaticFiles(directory=settings.static_dir, html=True), name="spa")

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    app = create_app(Settings())
    # host/port fixed, not env-driven (the Deployment disables service links).
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")


if __name__ == "__main__":
    main()
