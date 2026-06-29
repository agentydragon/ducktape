"""FastAPI app for Haku's own UI service: JSON API + same-origin React SPA.

This is **Haku-owned** starter code: it runs in ``haku-sandbox``, behind the
operator-owned Authentik outpost, embedded in the trusted console's "Free-form UI"
iframe. It is the **feature layer**: it knows which files in haku-state back each surface
and how to parse them, composing the feature-agnostic ``Forgejo`` git-content client
(``forgejo.py``) + the items-board read in ``reads.py``. Reads ``items/`` + ``improvements.yaml``;
writes ``clicks/`` / ``intake/`` back on operator action (the conventions Haku reduces on its
next run). No local clone.

Unlike the trusted console, there is **no capability tier** here — only the low-privilege
trace surface (clicks + feedback into haku-state, which Haku already owns). The privileged
launch-routine capability stays in the console.

This starter ships two person-agnostic surfaces: the **items board** (``/api/dashboard`` + the
trace writes) and Haku's **Improvements** self-backlog (``/api/improvements``). Haku adds more
endpoints here as it builds bespoke surfaces for its operator — those operator-specific
surfaces live in that operator's haku-state, not in this generic starter. Adding one never
touches ``forgejo.py``: it reads via the generic ``read_yaml``/``tree``/``blobs`` primitives.

**Operator authentication.** The app is only reachable through the Authentik outpost, which
injects ``X-authentik-username``. We read it to know who acted (logged on every write). The
header is only trustworthy once an ingress NetworkPolicy restricts the app to the outpost (so
a sibling haku-sandbox pod can't spoof it). Until then, treat it as advisory.
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
from models import Click, DashboardResponse, FeedbackRequest, ImprovementsBoard
from reads import read_dashboard

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
    async def _response_headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = "frame-ancestors https://haku.allegedly.works"
        # The SPA shell (index.html) must always revalidate: a new build references new hashed
        # asset filenames, so a stale-cached index.html keeps loading the old app (and the iframe
        # keeps showing the previous page). Hashed assets themselves stay cacheable.
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/dashboard")
    async def dashboard(forgejo: ForgejoDep) -> DashboardResponse:
        """Items (with their currently-clicked action ids), the last scan time, and the
        commit the running image was built from (for the footer's Forgejo link)."""
        items, clicks, scan_time = await read_dashboard(forgejo)
        clicked = [Click(item_id=item_id, action_id=action_id) for item_id, action_id in sorted(clicks)]
        sha = settings.git_sha
        return DashboardResponse(
            scan_time=scan_time,
            deployed_commit=sha[:7] if sha else None,
            deployed_commit_url=f"{settings.repo_web_url}/commit/{sha}" if sha else None,
            items=items,
            clicks=clicked,
        )

    # --- Improvements / friction surface: Haku's read-only self-backlog ---------
    @app.get("/api/improvements")
    async def improvements(forgejo: ForgejoDep) -> ImprovementsBoard:
        """Capability ideas + friction log (improvements.yaml). Read-only; empty board if absent."""
        raw = await forgejo.read_yaml("improvements.yaml")
        return ImprovementsBoard.model_validate(raw) if raw else ImprovementsBoard()

    # --- trace tier: operator-expressed intent recorded into haku-state ---------
    # A clicked action is the file clicks/<item_id>/<action_id> (removed on un-click); feedback
    # is an intake/ note. Haku reduces these on its next run. This layer owns the paths/messages;
    # the Forgejo client just makes idempotent single-file commits.

    @app.put("/api/trace/items/{item_id}/actions/{action_id}")
    async def set_click(item_id: str, action_id: str, forgejo: ForgejoDep, operator: Operator = None) -> dict[str, str]:
        logger.info("click %s on %s by %s", action_id, item_id, operator or "<unknown>")
        stamp = f"clicked_at: {dt.datetime.now(dt.UTC).isoformat(timespec='seconds')}\n"
        await forgejo.create_file(
            f"clicks/{item_id}/{action_id}", stamp.encode(), f"ui: click {action_id} on {item_id}"
        )
        return {"status": "clicked"}

    @app.delete("/api/trace/items/{item_id}/actions/{action_id}")
    async def clear_click(
        item_id: str, action_id: str, forgejo: ForgejoDep, operator: Operator = None
    ) -> dict[str, str]:
        logger.info("unclick %s on %s by %s", action_id, item_id, operator or "<unknown>")
        await forgejo.delete_file(f"clicks/{item_id}/{action_id}", f"ui: unclick {action_id} on {item_id}")
        return {"status": "cleared"}

    @app.post("/api/trace/feedback")
    async def feedback(req: FeedbackRequest, forgejo: ForgejoDep, operator: Operator = None) -> dict[str, str]:
        logger.info("feedback (item=%s) by %s", req.item_id or "<global>", operator or "<unknown>")
        # Per-item feedback names the item id in the note + filename; Haku's run contract treats
        # an intake note referencing an item id as feedback on it.
        stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        suffix = f"-{req.item_id}" if req.item_id else ""
        heading = f"Operator feedback on {req.item_id}" if req.item_id else "Operator feedback"
        message = f"ui: feedback on {req.item_id}" if req.item_id else "ui: feedback"
        body = f"# {heading} ({stamp})\n\n{req.text.strip()}\n"
        await forgejo.create_file(f"intake/{stamp}-feedback{suffix}.md", body.encode(), message)
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
