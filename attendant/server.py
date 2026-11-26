"""Serve one authenticated desktop viewer and independently scoped caller requests."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, Response
from python.runfiles import Runfiles
from starlette.requests import ClientDisconnect, HTTPConnection
from starlette.websockets import WebSocketState

from attendant.host import Host
from attendant.runtime import Operation, Reply, SessionError, valid_target

logger = logging.getLogger(__name__)

_SESSION_TTL_SECONDS = 8 * 60 * 60
_MAX_OWNER_BYTES = 4096
_MAX_REQUEST_BYTES = 1024 * 1024
_MAX_REPLY_BYTES = 16 * 1024 * 1024
_MAX_TARGET_LENGTH = 4096
_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"})
_ISSUE_STATUS = {
    "sign_in_needed": 409,
    "challenge_required": 409,
    "account_changed": 409,
    "binding_required": 409,
    "busy": 409,
    "denied": 403,
    "upstream_error": 502,
}
_VIEWER = "ducktape/attendant/viewer/dist"
_AUTHORITY = re.compile(r"(?:[a-zA-Z0-9.-]+|\[[a-fA-F0-9:.]+\])(?::[0-9]{1,5})?")


@dataclass
class OwnerSession:
    key: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    expires_at: float = field(default_factory=lambda: time.monotonic() + _SESSION_TTL_SECONDS)
    revoked: asyncio.Event = field(default_factory=asyncio.Event)


def _equal(supplied: str, expected: str) -> bool:
    return supplied.isascii() and hmac.compare_digest(supplied, expected)


def _normalize_origin(scheme: str, authority: str) -> str | None:
    if scheme not in {"http", "https"} or not _AUTHORITY.fullmatch(authority):
        return None
    try:
        parts = urlsplit(f"{scheme}://{authority}")
        port = parts.port
    except ValueError:
        return None
    hostname = parts.hostname
    if hostname is None:
        return None
    address = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 443 if scheme == "https" else 80
    if port is not None and port != default_port:
        address += f":{port}"
    return f"{scheme}://{address}"


def _has_bearer(connection: HTTPConnection, token: str) -> bool:
    supplied = connection.headers.getlist("authorization")
    return len(supplied) == 1 and _equal(supplied[0], f"Bearer {token}")


async def _read_json(request: Request, limit: int) -> dict[str, object]:
    content_types = request.headers.getlist("content-type")
    if len(content_types) != 1 or content_types[0].partition(";")[0].strip().lower() != "application/json":
        raise HTTPException(status_code=415, detail="json_required")
    body = bytearray()
    try:
        async for chunk in request.stream():
            if len(body) + len(chunk) > limit:
                raise HTTPException(status_code=413, detail="request_too_large")
            body.extend(chunk)
        value = json.loads(body.decode("utf-8"))
    except ValueError, RecursionError, ClientDisconnect:
        raise HTTPException(status_code=400, detail="invalid_request") from None
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="invalid_request")
    return cast(dict[str, object], value)


async def _invoke[T](operation: Awaitable[T]) -> T:
    try:
        return await operation
    except SessionError as error:
        code = error.code if error.code in _ISSUE_STATUS else "upstream_error"
        raise HTTPException(status_code=_ISSUE_STATUS[code], detail=code) from None
    except Exception as error:
        logger.error("attendant host operation failed error_type=%s", type(error).__name__)  # noqa: TRY400
        raise HTTPException(status_code=502, detail="upstream_error") from None


def _reply_response(reply: Reply) -> Response:
    if not 200 <= reply.status <= 599 or len(reply.body) > _MAX_REPLY_BYTES:
        raise HTTPException(status_code=502, detail="upstream_error")
    content_type = reply.content_type.partition(";")[0].strip().lower()
    if content_type == "application/json" or content_type.endswith("+json"):
        media_type = "application/json"
    elif content_type == "text/plain":
        media_type = "text/plain"
    else:
        media_type = "application/octet-stream"
    body = b"" if reply.status in {204, 205, 304} else reply.body
    return Response(
        content=body, status_code=reply.status, media_type=media_type, headers={"Content-Disposition": "attachment"}
    )


def _viewer_asset(filename: str, media_type: str) -> FileResponse:
    path = Runfiles.CreateOrRaise().Rlocation(f"{_VIEWER}/{filename}")
    if path is None or not Path(path).is_file():
        raise HTTPException(status_code=503, detail="viewer_unavailable")
    return FileResponse(Path(path), media_type=media_type)


def create_app(
    host: Host, owner_token: str, route_tokens: dict[str, str], *, public_origin: str | None = None
) -> FastAPI:
    """Create the owner UI and caller API. Configure public_origin for a TLS terminator."""
    route_tokens = dict(route_tokens)
    tokens = {owner_token, *route_tokens.values()}
    if (
        route_tokens.keys() != host.providers.keys()
        or len(tokens) != len(route_tokens) + 1
        or any(not token or any(not 33 <= ord(char) <= 126 for char in token) for token in tokens)
    ):
        raise ValueError("each provider and owner needs a distinct nonempty ASCII token")
    if public_origin is not None:
        scheme, _, authority = public_origin.partition("://")
        if not public_origin or _normalize_origin(scheme, authority) != public_origin:
            raise ValueError("public_origin must be a canonical HTTP origin")

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await host.start()
        try:
            yield
        finally:
            await host.close()

    app = FastAPI(lifespan=lifespan, openapi_url=None, docs_url=None, redoc_url=None, redirect_slashes=False)
    session: OwnerSession | None = None

    def origin(connection: HTTPConnection) -> str | None:
        if public_origin is not None:
            return public_origin
        scheme = connection.url.scheme
        scheme = {"ws": "http", "wss": "https"}.get(scheme, scheme)
        hosts = connection.headers.getlist("host")
        return _normalize_origin(scheme, hosts[0]) if len(hosts) == 1 else None

    def same_origin(connection: HTTPConnection) -> bool:
        expected = origin(connection)
        return (
            expected is not None
            and connection.headers.getlist("origin") == [expected]
            and connection.headers.getlist("sec-fetch-site") in ([], ["same-origin"])
            and not connection.scope.get("query_string")
        )

    def owner_cookie(connection: HTTPConnection) -> tuple[str, bool]:
        expected = origin(connection)
        secure = expected is not None and expected.startswith("https://")
        return ("__Host-attendant_owner" if secure else "attendant_owner", secure)

    def signed_in(connection: HTTPConnection) -> OwnerSession | None:
        cookie, _ = owner_cookie(connection)
        if (
            session is not None
            and time.monotonic() < session.expires_at
            and _equal(connection.cookies.get(cookie, ""), session.key)
        ):
            return session
        return None

    def require_owner(request: Request) -> None:
        if signed_in(request) is None:
            raise HTTPException(status_code=401, detail="owner_sign_in_required")
        if request.method != "GET" and not same_origin(request):
            raise HTTPException(status_code=403, detail="invalid_origin")

    def require_provider(provider: str) -> None:
        if provider not in host.providers:
            raise HTTPException(status_code=404, detail="unknown_provider")

    @app.middleware("http")
    async def secure_headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers.setdefault("Content-Security-Policy", "default-src 'none'; sandbox; frame-ancestors 'none'")
        return response

    @app.get("/")
    async def index(request: Request) -> FileResponse:
        response = _viewer_asset("index.html", "text/html")
        expected = origin(request)
        websocket_origin = "" if expected is None else " ws" + expected.removeprefix("http")
        response.headers["Content-Security-Policy"] = (
            f"default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'{websocket_origin}; "
            "img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        )
        return response

    @app.get("/main.js")
    async def javascript() -> FileResponse:
        return _viewer_asset("main.js", "application/javascript")

    @app.get("/main.css")
    async def stylesheet() -> FileResponse:
        return _viewer_asset("main.css", "text/css")

    @app.get("/healthz")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/owner/login")
    async def login(request: Request) -> Response:
        nonlocal session
        if not same_origin(request):
            raise HTTPException(status_code=403, detail="invalid_origin")
        payload = await _read_json(request, _MAX_OWNER_BYTES)
        token = payload.get("token")
        if set(payload) != {"token"} or not isinstance(token, str) or not _equal(token, owner_token):
            raise HTTPException(status_code=401, detail="invalid_token")
        if session is not None:
            session.revoked.set()
        session = OwnerSession()
        response = Response(status_code=204)
        cookie, secure = owner_cookie(request)
        response.set_cookie(
            cookie, session.key, max_age=_SESSION_TTL_SECONDS, secure=secure, httponly=True, samesite="strict"
        )
        return response

    @app.post("/owner/logout")
    async def logout(request: Request) -> Response:
        nonlocal session
        require_owner(request)
        if session is not None:
            session.revoked.set()
            session = None
        response = Response(status_code=204)
        cookie, secure = owner_cookie(request)
        response.delete_cookie(cookie, secure=secure, httponly=True, samesite="strict")
        return response

    @app.get("/owner/providers")
    async def providers(request: Request) -> dict[str, list[str]]:
        require_owner(request)
        return {"providers": sorted(host.providers)}

    @app.post("/owner/providers/{provider}/open")
    async def open_provider(request: Request, provider: str) -> Response:
        require_owner(request)
        require_provider(provider)
        await _invoke(host.open(provider))
        return Response(status_code=204)

    @app.post("/owner/providers/{provider}/verify")
    async def verify_provider(request: Request, provider: str) -> dict[str, str]:
        require_owner(request)
        require_provider(provider)
        payload = await _read_json(request, _MAX_OWNER_BYTES)
        require_owner(request)
        bind = payload.get("bind", False)
        if set(payload) - {"bind"} or not isinstance(bind, bool):
            raise HTTPException(status_code=400, detail="invalid_request")
        state = await _invoke(host.status(provider, bind=bind))
        return {"state": state if state == "ready" or state in _ISSUE_STATUS else "upstream_error"}

    @app.websocket("/owner/viewer")
    async def viewer(websocket: WebSocket) -> None:
        current = signed_in(websocket)
        if current is None or websocket.headers.getlist("authorization"):
            await websocket.close(code=4401)
            return
        if not same_origin(websocket):
            await websocket.close(code=4403)
            return

        # Build the private credential only after validating the browser's cookie and origin.
        scope = {**websocket.scope, "headers": [(b"authorization", f"Bearer {owner_token}".encode("ascii"))]}
        upstream = WebSocket(scope=scope, receive=websocket.receive, send=websocket.send)
        relay = asyncio.create_task(host.websocket(upstream))
        revoked = asyncio.create_task(current.revoked.wait())
        close_code = 1000
        try:
            await asyncio.wait(
                (relay, revoked),
                timeout=max(0, current.expires_at - time.monotonic()),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if relay.done():
                await relay
            else:
                close_code = 4401
        except Exception as error:
            logger.error("attendant viewer failed error_type=%s", type(error).__name__)  # noqa: TRY400
            close_code = 1011
        finally:
            relay.cancel()
            revoked.cancel()
            await asyncio.gather(relay, revoked, return_exceptions=True)
            if websocket.application_state is not WebSocketState.DISCONNECTED:
                with suppress(OSError, RuntimeError):
                    await websocket.close(code=close_code)

    @app.post("/api/providers/{provider}/request")
    async def provider_request(request: Request, provider: str) -> Response:
        token = route_tokens.get(provider)
        if token is None or not _has_bearer(request, token):
            raise HTTPException(status_code=401, detail="invalid_token", headers={"WWW-Authenticate": "Bearer"})
        if request.scope.get("query_string"):
            raise HTTPException(status_code=400, detail="invalid_request")
        payload = await _read_json(request, _MAX_REQUEST_BYTES)
        method = payload.get("method")
        target = payload.get("target")
        body = payload.get("body", "")
        if (
            set(payload) - {"method", "target", "body"}
            or not isinstance(method, str)
            or method not in _METHODS
            or not isinstance(target, str)
            or len(target) > _MAX_TARGET_LENGTH
            or not valid_target(target)
            or not isinstance(body, str)
            or (method in {"GET", "HEAD"} and body)
        ):
            raise HTTPException(status_code=400, detail="invalid_request")
        try:
            contents = body.encode("utf-8")
        except UnicodeError:
            raise HTTPException(status_code=400, detail="invalid_request") from None
        return _reply_response(
            await _invoke(host.request(provider, Operation(method=method, target=target, body=contents)))
        )

    return app
