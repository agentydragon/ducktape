from collections.abc import Iterator
import json
from typing import Any

import pytest

from adgn.mcp.gitea_mirror import server
from adgn.mcp.gitea_mirror.server import TriggerMirrorSyncArgs


class _DummyResponse:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(self.text or f"HTTP {self.status_code}")


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITEA_BASE_URL", raising=False)
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    monkeypatch.delenv("GITEA_POLL_INTERVAL_SECS", raising=False)
    monkeypatch.delenv("GITEA_POLL_TIMEOUT_SECS", raising=False)


def _iter(values: list[float]) -> Iterator[float]:
    yield from values
    while True:  # pragma: no cover - defensive fallback
        yield values[-1]


def _extract_payload(result):
    if isinstance(result, dict):
        return result
    if isinstance(result, tuple) and len(result) == 2:
        blocks, payload = result
        assert isinstance(blocks, list), f"Expected content blocks list, got {type(blocks)}"
        assert payload is None or isinstance(payload, dict), f"Expected dict payload, got {type(payload)}"
        if payload is not None:
            return payload
        assert blocks, "Expected at least one block"
        assert blocks[0].type == "text", "Expected text content block"
        return json.loads(blocks[0].text)
    raise AssertionError(f"Unexpected tool response: {result!r}")


async def test_trigger_mirror_sync_success(monkeypatch: pytest.MonkeyPatch, make_typed_mcp) -> None:
    post_calls: list[tuple[str, dict, dict]] = []

    def fake_post(url: str, **kwargs: Any):
        headers = kwargs.get("headers", {})
        payload = kwargs.get("json", {})
        post_calls.append((url, headers, payload))
        if url.endswith("/repos/migrate"):
            return _DummyResponse(201)
        if url.endswith("/mirror-sync"):
            return _DummyResponse(200)
        raise AssertionError(f"Unexpected POST {url}")

    monkeypatch.setattr(server.requests, "post", fake_post)
    monkeypatch.setattr(server, "_resolve_owner", lambda *_: "mirror-user")

    mirror_server = server.make_gitea_mirror_server(base_url="https://gitea.local", token="secret-token")

    async with make_typed_mcp(mirror_server, "gitea_mirror") as (client, _):
        await client.trigger_mirror_sync(TriggerMirrorSyncArgs(url="https://example.com/org/repo.git"))

    assert [call[0] for call in post_calls] == [
        "https://gitea.local/api/v1/repos/migrate",
        "https://gitea.local/api/v1/repos/mirror-user/example-com-org-repo/mirror-sync",
    ]
    migrate_headers = post_calls[0][1]
    assert migrate_headers["Authorization"] == "token secret-token"


async def test_trigger_sync_bubbles_mirror_error(monkeypatch: pytest.MonkeyPatch, make_typed_mcp) -> None:
    def fake_post(url: str, **kwargs: Any):
        if url.endswith("/repos/migrate"):
            return _DummyResponse(500, text="boom")
        raise AssertionError("mirror-sync should not be called")

    monkeypatch.setattr(server.requests, "post", fake_post)
    monkeypatch.setattr(server, "_resolve_owner", lambda *_: "mirror-user")

    def unexpected_get(*_, **__):  # pragma: no cover - helper
        raise AssertionError("GET not expected")

    monkeypatch.setattr(server.requests, "get", unexpected_get)

    mirror_server = server.make_gitea_mirror_server(base_url="https://gitea.local", token="secret-token")

    async with make_typed_mcp(mirror_server, "gitea_mirror") as (client, _):
        # Error assertion path: expect tool error and capture message
        err_msg = await client.error("trigger_mirror_sync")(TriggerMirrorSyncArgs(url="https://example.com/org/repo"))
        assert "boom" in err_msg or "HTTP 500" in err_msg


def test_make_mcp_requires_configuration() -> None:
    # Use a raw regex for the generic message match
    with pytest.raises(ValueError, match=r"."):
        server.make_gitea_mirror_server()
