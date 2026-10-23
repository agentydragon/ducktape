"""Exercise nginx's TLS/reload path through the held TCP-to-Unix relay."""

import asyncio
import ssl
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_bazel
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from agentplane.app.testing.http2_proxy import browser_certificate, http2_proxy
from util.testing.asgi import serve_app_in_loop


async def test_tls_http2_and_reload_keep_the_same_owned_endpoint(tmp_path: Path) -> None:
    async def models(_request: Request) -> JSONResponse:
        return JSONResponse({"models": []})

    async def stream(_request: Request) -> StreamingResponse:
        async def body() -> AsyncIterator[bytes]:
            yield b"data: test-event\n\n"
            await asyncio.Event().wait()

        return StreamingResponse(body(), media_type="text/event-stream")

    certificate = browser_certificate(tmp_path / "certificate")
    context = ssl.create_default_context(cafile=str(certificate.directory / "certificate.pem"))
    app = Starlette(routes=[Route("/readyz", models), Route("/models", models), Route("/stream", stream)])
    async with serve_app_in_loop(app) as origin, http2_proxy(origin, certificate) as ingress:
        async with asyncio.timeout(15), httpx.AsyncClient(verify=context, http2=True) as client:
            response = await client.get(f"{ingress.url}/models")
            assert response.http_version == "HTTP/2"
            assert response.json() == {"models": []}
            async with client.stream("GET", f"{ingress.url}/stream") as response:
                assert response.http_version == "HTTP/2"
                lines = response.aiter_lines()
                assert await anext(lines) == "data: test-event"
                assert await anext(lines) == ""
                await ingress.drop_connections()
                with pytest.raises((StopAsyncIteration, httpx.RemoteProtocolError, httpx.ReadError)):
                    await anext(lines)
        async with httpx.AsyncClient(verify=context, http2=True) as client:
            response = await client.get(f"{ingress.url}/models")
            assert response.status_code == 200
            assert response.http_version == "HTTP/2"


if __name__ == "__main__":
    pytest_bazel.main()
