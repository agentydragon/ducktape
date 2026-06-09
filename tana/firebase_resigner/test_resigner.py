from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest_bazel

from tana.firebase_resigner.resigner import ResignerConfig, _is_tana_ready, _pat_accepted


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_pat_accepted_true_on_200_and_terminates_session() -> None:
    deleted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            assert request.headers["authorization"] == "Bearer pat-123"
            return httpx.Response(
                200, headers={"mcp-session-id": "sess-1"}, json={"jsonrpc": "2.0", "id": 1, "result": {}}
            )
        if request.method == "DELETE":
            deleted.append(request.headers["mcp-session-id"])
            return httpx.Response(200)
        raise AssertionError(f"unexpected method {request.method}")

    cfg = ResignerConfig(api_key="k", pat="pat-123")
    async with _client(handler) as http:
        assert await _pat_accepted(http, cfg) is True
    # The successful probe's session was terminated so it doesn't leak per poll.
    assert deleted == ["sess-1"]


async def test_pat_accepted_false_on_401() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"jsonrpc": "2.0", "error": {"code": -32001}, "id": None})

    cfg = ResignerConfig(api_key="k", pat="stale")
    async with _client(handler) as http:
        assert await _pat_accepted(http, cfg) is False


async def test_ready_false_when_health_ok_but_pat_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(401)

    cfg = ResignerConfig(api_key="k", pat="stale")
    async with _client(handler) as http:
        assert await _is_tana_ready(http, cfg) is False


async def test_ready_uses_health_only_when_no_pat_configured() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # Without a configured PAT the resigner must not probe /mcp at all.
        assert request.url.path == "/health"
        return httpx.Response(200, json={"status": "ok"})

    cfg = ResignerConfig(api_key="k")
    async with _client(handler) as http:
        assert await _is_tana_ready(http, cfg) is True


if __name__ == "__main__":
    pytest_bazel.main()
