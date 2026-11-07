"""Provider-neutral management service for owned upstream sessions."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from platform_sessions.auth import BearerAuth
from platform_sessions.limits import MAX_SESSION_BYTES
from platform_sessions.registry import IntegrationInstance

logger = logging.getLogger(__name__)
_FRONTEND = Path(__file__).parent / "frontend"
_CSP = "default-src 'none'; script-src 'self'; connect-src 'self'; style-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"


class IntegrationError(Exception):
    """A sanitized outcome that an integration wants to show its owner."""

    def __init__(self, code: str, status: int):
        super().__init__(code)
        self.code = code
        self.status = status


class Integration(Protocol):
    """Management operations implemented by one configured integration."""

    config: IntegrationInstance
    periodic: bool
    candidate_verification: bool

    async def metadata(self) -> dict[str, Any]: ...
    def parse(self, body: bytes) -> Any: ...
    async def status(self) -> dict[str, Any]: ...
    async def refresh(self, transport: Any = None, *, source: str = "verify") -> None: ...
    async def install(self, candidate: Any, transport: Any = None) -> dict[str, Any]: ...


def _failure(
    exc: Exception, default: tuple[str, int], classify: Callable[[Exception], tuple[str, int] | None] | None
) -> JSONResponse:
    if isinstance(exc, IntegrationError):
        code, status = exc.code, exc.status
    else:
        code, status = (classify(exc) if classify else None) or default
    return JSONResponse({"error": code}, status_code=status)


def create_management_router(
    integrations: Sequence[Integration],
    public_base_url: str,
    *,
    require_owner: Callable[..., Any],
    auth: BearerAuth,
    transport: Any = None,
    classify_error: Callable[[Exception], tuple[str, int] | None] | None = None,
) -> APIRouter:
    """Create owner-authenticated routes; authentication is supplied by the host."""
    by_id = {integration.config.id: integration for integration in integrations}
    if len(by_id) != len(integrations):
        raise ValueError("Duplicate integration ID")

    async def require_management_owner(request: Request) -> None:
        if request.headers.getlist("authorization"):
            principal = auth.authenticate(request)
            if principal is None or not principal.owner:
                raise HTTPException(status_code=403, detail="Owner access required")
            return
        result = require_owner(request)
        if inspect.isawaitable(result):
            await result

    router = APIRouter(prefix="/api/integrations", dependencies=[Depends(require_management_owner)])

    def same_origin(request: Request) -> bool:
        origin = request.headers.get("origin")
        principal = auth.authenticate(request)
        return request.headers.get("sec-fetch-site") in {None, "same-origin", "none"} and (
            origin == public_base_url.rstrip("/") or (origin is None and principal is not None and principal.owner)
        )

    async def candidate_request(id: str, request: Request) -> tuple[Integration | None, Any, JSONResponse | None]:
        runtime = by_id.get(id)
        if runtime is None:
            return None, None, JSONResponse({"error": "not_found"}, status_code=404)
        if not same_origin(request):
            return None, None, JSONResponse({"error": "invalid_origin"}, status_code=403)
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            return None, None, JSONResponse({"error": "invalid_session"}, status_code=400)
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_SESSION_BYTES:
                return None, None, JSONResponse({"error": "invalid_session"}, status_code=400)
            body.extend(chunk)
        try:
            candidate = runtime.parse(bytes(body))
        except ValueError, TypeError, UnicodeError:
            return None, None, JSONResponse({"error": "invalid_session"}, status_code=400)
        except Exception:
            return None, None, JSONResponse({"error": "invalid_session"}, status_code=400)
        return runtime, candidate, None

    @router.get("")
    async def list_integrations():
        return await asyncio.gather(*(runtime.metadata() for runtime in integrations))

    @router.get("/{id}")
    async def get_integration(id: str):
        runtime = by_id.get(id)
        if runtime is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return await runtime.status()

    @router.post("/{id}/verify")
    async def verify_installed(id: str, request: Request):
        runtime = by_id.get(id)
        if runtime is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        if not same_origin(request):
            return JSONResponse({"error": "invalid_origin"}, status_code=403)
        try:
            await runtime.refresh(transport)
        except Exception as exc:
            return _failure(exc, ("verification_failed", 503), classify_error)
        return await runtime.status()

    @router.post("/{id}/session/verify")
    async def verify_candidate(id: str, request: Request):
        runtime, candidate, error = await candidate_request(id, request)
        if error is not None:
            return error
        assert runtime is not None
        check = getattr(runtime, "check_candidate", None)
        if not runtime.candidate_verification or check is None:
            return JSONResponse({"error": "not_supported"}, status_code=404)
        try:
            await check(candidate, transport)
        except Exception as exc:
            return _failure(exc, ("outcome_unknown", 503), classify_error)
        return {"outcome": "success"}

    @router.post("/{id}/session")
    async def import_session(id: str, request: Request):
        runtime, candidate, error = await candidate_request(id, request)
        if error is not None:
            return error
        assert runtime is not None
        try:
            return await runtime.install(candidate, transport)
        except Exception as exc:
            return _failure(exc, ("outcome_unknown", 503), classify_error)

    return router


@contextlib.asynccontextmanager
async def poll_integrations(integrations: Sequence[Integration], transport: Any = None):
    """Run each opted-in provider's existing poll operation at its interval."""

    async def poll(runtime: Integration) -> None:
        while True:
            try:
                await runtime.refresh(transport, source="periodic")
            except Exception:
                logger.error("Integration poll failed")  # noqa: TRY400 - exceptions may contain session data
            await asyncio.sleep(runtime.config.poll_interval_seconds)

    tasks = [asyncio.create_task(poll(runtime)) for runtime in integrations if runtime.periodic]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


def create_app(
    integrations: Sequence[Integration],
    public_base_url: str,
    *,
    require_owner: Callable[..., Any],
    auth: BearerAuth,
    transport: Any = None,
    classify_error: Callable[[Exception], tuple[str, int] | None] | None = None,
    start: Callable[[], Awaitable[None]] | None = None,
    stop: Callable[[], Awaitable[None]] | None = None,
) -> FastAPI:
    """Build a host; callers install owner login and provider-specific routes."""

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            if start is not None:
                await start()
            async with poll_integrations(integrations, transport):
                yield
        finally:
            if stop is not None:
                await stop()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.include_router(
        create_management_router(
            integrations,
            public_base_url,
            require_owner=require_owner,
            auth=auth,
            transport=transport,
            classify_error=classify_error,
        )
    )

    @app.middleware("http")
    async def disable_caching(request: Request, call_next):
        response = await call_next(request)
        if not request.url.path.startswith("/proxy/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return HTMLResponse(
            (_FRONTEND / "status.html").read_text(encoding="utf-8"),
            headers={"Referrer-Policy": "no-referrer", "Content-Security-Policy": _CSP},
        )

    @app.get("/status.css")
    async def stylesheet():
        return Response((_FRONTEND / "status.css").read_text(encoding="utf-8"), media_type="text/css")

    @app.get("/status.js")
    async def javascript():
        return Response((_FRONTEND / "status.js").read_text(encoding="utf-8"), media_type="application/javascript")

    return app
