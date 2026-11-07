"""Owner-authenticated transport to the generated management HTTP facade."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from platform_sessions.auth import BearerAuth
from platform_sessions.host import owner_authorizer, same_origin
from platform_sessions.limits import MAX_SESSION_BYTES


@contextlib.asynccontextmanager
async def run_facade(executable: Path, target: str) -> AsyncIterator[str]:
    """Run the generated facade as a loopback-only child of the owner gateway."""
    process = await asyncio.create_subprocess_exec(
        str(executable), "--grpc-target", target, stdout=asyncio.subprocess.PIPE
    )
    try:
        assert process.stdout is not None
        address = (await asyncio.wait_for(process.stdout.readline(), timeout=5)).decode("ascii").strip()
        host, separator, port = address.rpartition(":")
        try:
            valid_address = bool(separator and port.isdecimal() and ipaddress.ip_address(host).is_loopback)
        except ValueError:
            valid_address = False
        if not valid_address:
            raise RuntimeError("Management facade did not provide a loopback address")
        yield f"http://{address}"
    finally:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                process.kill()
                await process.wait()


def create_router(facade_url: str, token_file: Path, public_base_url: str, *, require_owner, auth: BearerAuth):
    parsed = urlsplit(facade_url)
    if parsed.scheme != "http" or not parsed.hostname or not ipaddress.ip_address(parsed.hostname).is_loopback:
        raise ValueError("Management facade must use loopback HTTP")
    router = APIRouter(prefix="/api/v1/instances", dependencies=[Depends(owner_authorizer(require_owner, auth))])

    @router.api_route("", methods=["GET"])
    @router.api_route("/{path:path}", methods=["GET", "POST"])
    async def forward(request: Request, path: str = ""):
        if request.method != "GET" and not same_origin(request, public_base_url, auth):
            return JSONResponse({"error": "invalid_origin"}, status_code=403)
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_SESSION_BYTES * 2:
                return JSONResponse({"error": "request_too_large"}, status_code=413)
            body.extend(chunk)
        try:
            token = (await asyncio.to_thread(token_file.read_text, encoding="ascii")).strip()
            if len(token) < 32 or any(not 33 <= ord(char) <= 126 for char in token):
                raise ValueError("Invalid management bearer")
            # Explicitly disable proxy configuration and redirects. Never retry a mutation.
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=35) as client:
                response = await client.request(
                    request.method,
                    facade_url + request.url.path,
                    content=bytes(body),
                    headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
                )
        except OSError, ValueError, httpx.HTTPError:
            return JSONResponse(
                {"error": "outcome_unknown" if request.method != "GET" else "unavailable"}, status_code=503
            )
        return Response(response.content, status_code=response.status_code, media_type="application/json")

    return router
