"""Tests for registry proxy header forwarding.

Verifies that multi-valued headers (especially Accept) are preserved
when proxying requests to the upstream registry. Docker sends multiple
Accept header lines for manifest requests, and all values must be
forwarded to the upstream registry.
"""

from __future__ import annotations

import asyncio
import contextlib

import httpx
import pytest
import pytest_bazel
import uvicorn
from fastapi import FastAPI, Request
from starlette.responses import Response

from props.backend.routes.registry import _proxy_to_upstream

DOCKER_ACCEPT_TYPES = [
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.oci.image.index.v1+json",
]


@contextlib.asynccontextmanager
async def _run_server(app: FastAPI):
    """Run a FastAPI app on an ephemeral port, yield the base URL."""
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)

    started = asyncio.Event()
    original_startup = server.startup

    async def _startup_then_signal(**kwargs):
        await original_startup(**kwargs)
        started.set()

    server.startup = _startup_then_signal
    task = asyncio.create_task(server.serve())
    await asyncio.wait_for(started.wait(), timeout=5.0)

    # Extract the bound port
    sock = server.servers[0].sockets[0]
    port = sock.getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


async def test_proxy_preserves_multi_valued_accept_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that multiple Accept header values are all forwarded to upstream.

    Docker 28 sends separate Accept header lines for each manifest media type.
    The proxy must forward all of them, not just the last one (which is what
    dict(request.headers) does — it deduplicates keys, keeping last-value-wins).

    This test:
    1. Starts a fake upstream registry server that captures received headers
    2. Starts a proxy server that forwards via _proxy_to_upstream
    3. Sends a request through the proxy with multiple Accept headers
    4. Asserts the upstream received all Accept values
    """
    captured: list[list[tuple[str, str]]] = []

    upstream_app = FastAPI()

    @upstream_app.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH"])
    async def catch_all(request: Request) -> Response:
        captured.append(list(request.headers.items()))
        return Response(content=b'{"schemaVersion": 2}', status_code=200)

    proxy_app = FastAPI()

    @proxy_app.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH"])
    async def proxy(request: Request) -> Response:
        return await _proxy_to_upstream(request)

    async with _run_server(upstream_app) as upstream_url:
        monkeypatch.setenv("PROPS_REGISTRY_UPSTREAM_URL", upstream_url)

        async with _run_server(proxy_app) as proxy_url, httpx.AsyncClient(base_url=proxy_url) as client:
            raw_headers = [(b"accept", t.encode()) for t in DOCKER_ACCEPT_TYPES]
            response = await client.get("/v2/critic/manifests/sha256:abc123", headers=raw_headers)

    assert response.status_code == 200
    assert len(captured) == 1, f"Expected 1 upstream request, got {len(captured)}"

    # Extract all Accept values the upstream received
    upstream_accept_values = [v for k, v in captured[0] if k.lower() == "accept"]

    # All values may arrive as separate headers or comma-joined; check all types present
    all_accept = ", ".join(upstream_accept_values)
    for media_type in DOCKER_ACCEPT_TYPES:
        assert media_type in all_accept, (
            f"Accept type {media_type!r} was lost during proxying. Upstream received Accept: {upstream_accept_values!r}"
        )


if __name__ == "__main__":
    pytest_bazel.main()
