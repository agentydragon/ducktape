"""FastAPI app for Haku's own UI service: JSON API + same-origin React SPA.

This is **Haku-owned** starter code: it runs in the agent's namespace, behind an
operator-owned auth proxy, embedded in the trusted console's "Free-form UI" iframe. It
is the **feature layer**: it knows which files in haku-state back each surface and how
to parse them, composing the feature-agnostic ``Forgejo`` git-content client
(``forgejo.py``) + the batched tree/blob reads in ``reads.py``. It reads the content
collections and writes ``responses/`` / ``intake/`` back on operator action (the
conventions Haku reduces on its next run), all through the Forgejo API — no local
clone. Adding a surface never touches ``forgejo.py``: it reads via the generic
``read_yaml``/``tree``/``blobs`` primitives and writes via ``create_file``/``write_file``/``delete_file``.

There is **no capability tier** here — only the low-privilege trace surface (responses +
feedback into haku-state, which Haku already owns). Any privileged launch-routine
capability stays in the console.

**Operator authentication.** The app is only reachable through the auth proxy, which
injects ``X-authentik-username``. We read it to know who acted (logged on every write).
The header is only trustworthy once an ingress NetworkPolicy restricts the app to the
proxy (so a sibling pod in the same namespace can't spoof it). Until then, treat the
header as advisory.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Annotated

import canonicaljson
import httpx
import uvicorn
import yaml
from config import Settings
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from forgejo import Forgejo
from models import (
    FeedbackRequest,
    LocationRequest,
    MetaResponse,
    RepoBlob,
    RepoTree,
    RepoTreeEntry,
    ResponseRequest,
    ToolCallRecord,
    ToolRequestCallRequest,
    ToolRequestDoc,
)
from reads import read_scan_time

logger = logging.getLogger(__name__)

# The Authentik outpost injects X-authentik-username (advisory until the ingress
# NetworkPolicy lands — see the module docstring). None when called outside the outpost.
Operator = Annotated[str | None, Header(alias="X-authentik-username")]


def _response_path(scope: str, field: str) -> str:
    """The haku-state path for an operator-answer slot, validating both path segments (each a
    lowercase-slug directory/file name)."""
    for seg in (scope, field):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", seg):
            raise ValueError(f"invalid response path segment: {seg!r}")
    return f"responses/{scope}/{field}.yaml"


def _blob_sha(sha: str) -> str:
    """Validate a blob sha for the bulk-fetch proxy: lowercase hex only. Guards the
    comma-joined ``?shas=`` passed to Forgejo against injection; a bad sha is a 400, not a 500."""
    if not re.fullmatch(r"[0-9a-f]{4,64}", sha):
        raise HTTPException(status_code=400, detail=f"invalid blob sha: {sha!r}")
    return sha


def _state_request_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
        raise HTTPException(status_code=400, detail=f"invalid tool request id: {value!r}")
    return value


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
        response.headers["Content-Security-Policy"] = "frame-ancestors https://haku.example.com"
        # The SPA shell (index.html) must always revalidate: a new build references new hashed
        # asset filenames, so a stale-cached index.html keeps loading the old app (and the iframe
        # keeps showing the previous page). Hashed assets themselves stay cacheable.
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/meta")
    async def meta(forgejo: ForgejoDep) -> MetaResponse:
        """Footer metadata: the last-scan time + the commit the running image was built from. Items
        are read from `items/*.md` through the generic proxy (the frontend composes them)."""
        sha = settings.git_sha
        return MetaResponse(
            scan_time=await read_scan_time(forgejo),
            deployed_commit=sha[:7] if sha else None,
            deployed_commit_url=f"{settings.repo_web_url}/commit/{sha}" if sha else None,
        )

    # --- Responses surface: operator-answer slot writes ---------------------------
    # A generic keyed current-state file per (scope, field): the file at HEAD is the current answer,
    # the git commit history is the append-only log (plans/ui-authoring-architecture.md → feedback
    # loop). Replace-in-slot — the item status slot and forms compose over it. Reads go through the
    # generic proxy; writes stay here.
    @app.put("/api/responses/{scope}/{field}")
    async def set_response(
        scope: str, field: str, req: ResponseRequest, forgejo: ForgejoDep, operator: Operator = None
    ) -> dict[str, str]:
        logger.info("response %s/%s = %s by %s", scope, field, req.value, operator or "<unknown>")
        record = {"value": req.value, "at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds")}
        if req.note is not None:
            record["note"] = req.note
        content = yaml.safe_dump(record)
        await forgejo.write_file(
            _response_path(scope, field), content.encode(), f"ui: response {scope}/{field} = {req.value}"
        )
        return {"status": "ok"}

    @app.delete("/api/responses/{scope}/{field}")
    async def clear_response(scope: str, field: str, forgejo: ForgejoDep, operator: Operator = None) -> dict[str, str]:
        logger.info("clear response %s/%s by %s", scope, field, operator or "<unknown>")
        await forgejo.delete_file(_response_path(scope, field), f"ui: clear response {scope}/{field}")
        return {"status": "cleared"}

    # --- Location: the operator's last known position (persistence = TODO) ---------
    # The UI captures the operator's location on load (consent mediated by the trusted console's
    # geolocation bridge) and POSTs it here. It deliberately does NOT go into git: location fixes
    # are dense, frequently-updated time-series data that would bloat haku-state's history. The
    # intended home is a time-series store in Haku's own namespace (haku-sandbox), which isn't
    # stood up yet — so for now this validates + logs the fix but does not persist it.
    # TODO(haku): stand up a time-series store in haku-sandbox and write fixes to it here. Haku
    # owns that namespace, so it wires this itself; until then the endpoint is receive-and-drop.
    @app.post("/api/location")
    async def record_location(req: LocationRequest, operator: Operator = None) -> dict[str, str]:
        logger.info(
            "location update received by %s [TODO: not persisted — awaiting time-series store]", operator or "<unknown>"
        )
        return {"status": "ok"}

    # Improvements are now a content collection (memory/improvements/<id>.md), served by the
    # generic tree+blobs proxy and rendered by the <improvement-board/> garden widget — no
    # bespoke endpoint. See plans/garden-gradient.md → Settled mechanism.

    # The runs surface (runs/<date>/<ulid>.md) composes over the generic tree+blobs proxy — the
    # frontend reads each run's manifest frontmatter + prose body and parses them (client.ts:fetchRuns).
    # No bespoke endpoint. RunManifest/RunsResponse stay in models.py as the wire contract.

    # The knowledge garden (browse + file read) now composes over the generic content proxy
    # below — the frontend filters the tree to the curated dirs and fetches blobs. No bespoke
    # garden endpoint or path whitelist; the raw read is repo-wide (haku-state holds no secrets).

    # --- Generic read-only content proxy: Forgejo's two read primitives, thinly passed ---
    # The frontend composes over these (filter the tree, bulk-fetch the blobs it wants), so new
    # collections/views need zero backend code and existing server-side reads can migrate onto
    # them. Read-only by construction (never Forgejo's write methods); the git-write cred stays
    # server-side. See plans/garden-gradient.md → Settled mechanism.
    @app.get("/api/repo/tree")
    async def repo_tree(forgejo: ForgejoDep) -> RepoTree:
        """The whole repo's recursive git tree at HEAD — mirrors Forgejo's git-trees API. No path
        input (so no traversal surface); the frontend filters by prefix/kind. Repo-pinned, and
        haku-state is single-author with no credentials, so the full listing is safe to expose."""
        sha = (await forgejo.commits(1))[0]["sha"]
        entries = [RepoTreeEntry(path=e["path"], type=e["type"], sha=e["sha"]) for e in await forgejo.tree(sha)]
        return RepoTree(sha=sha, entries=entries)

    @app.get("/api/repo/blobs")
    async def repo_blobs(shas: str, forgejo: ForgejoDep) -> list[RepoBlob]:
        """Bulk blob fetch by comma-separated sha — mirrors Forgejo's git/blobs (the client
        batches internally). Each sha is validated hex; content is UTF-8 text. 400 on a bad sha."""
        sha_list = [_blob_sha(s) for s in shas.split(",") if s]
        contents = await forgejo.blobs(sha_list)
        return [RepoBlob(sha=s, content=c.decode()) for s, c in zip(sha_list, contents, strict=True)]

    # --- Operator-approved tool calls ------------------------------------------
    # haku-state stores only the authored request. haku-console owns authorization,
    # execution, audit, and results; this backend is a same-origin proxy for haku-ui.
    @app.post("/api/tool-requests/{state_request_id}/call")
    async def call_tool_request(
        state_request_id: str, req: ToolRequestCallRequest, forgejo: ForgejoDep, operator: Operator = None
    ) -> ToolCallRecord:
        if settings.haku_console_api_url is None:
            raise HTTPException(status_code=503, detail="haku-console tool-call API is not configured")
        request_id = _state_request_id(state_request_id)
        path = f"tool_requests/{request_id}.yaml"
        raw = await forgejo.read_yaml(path)
        if raw is None:
            raise HTTPException(status_code=404, detail=f"tool request not found: {path}")
        doc = ToolRequestDoc.model_validate(raw)
        if doc.state_request_id != request_id:
            raise HTTPException(status_code=400, detail="tool request file id does not match path")
        digest = hashlib.sha256(canonicaljson.encode_canonical_json(doc.model_dump(mode="json"))).hexdigest()
        payload = {
            "server_id": doc.server_id,
            "tool_name": doc.tool_name,
            "arguments": doc.arguments,
            "rationale": doc.rationale,
            "request_title": doc.title,
            "state_request_id": doc.state_request_id,
            "client_request_id": f"haku-state:{path}@sha256:{digest}",
            "wait_for_ms": req.wait_for_ms,
        }
        headers: dict[str, str] = {}
        if settings.haku_console_api_token is not None:
            headers["Authorization"] = f"Bearer {settings.haku_console_api_token.get_secret_value()}"
        logger.info("tool request %s submitted to console by %s", request_id, operator or "<unknown>")
        async with httpx.AsyncClient(base_url=settings.haku_console_api_url, timeout=65.0) as http:
            resp = await http.post("/api/approvals/tool-calls", json=payload, headers=headers)
        if not resp.is_success:
            raise HTTPException(status_code=resp.status_code, detail=resp.text[:1000])
        return ToolCallRecord.model_validate(resp.json())

    # --- trace tier: operator feedback recorded into haku-state -----------------
    # A feedback note is an intake/ file Haku reduces on its next run. (Operator status/affordance
    # input goes through the responses/ endpoints above, not here.) This layer owns the
    # paths/messages; the Forgejo client just makes idempotent single-file commits.

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
        # Page + selection give Haku grounding for context-free notes ("this page looks bad"),
        # appended under a rule as clean markdown (selection block-quoted so it reads as a quote).
        context: list[str] = []
        if req.page:
            context.append(f"Reported from page: {req.page}")
        if req.selection and req.selection.strip():
            quoted = "\n".join(f"> {line}" for line in req.selection.strip().splitlines())
            context.append(f"Selected text:\n{quoted}")
        if context:
            body += "\n---\n" + "\n\n".join(context) + "\n"
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
