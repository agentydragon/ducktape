"""The generic Forgejo content client — git primitives, no feature knowledge.

Forgejo is mocked with httpx.MockTransport, so these are pure unit tests of forgejo.py:
the batched tree/blobs reads (incl. the truncated-tree guard and the 80-SHA chunking),
single-file read_text/read_yaml, and the idempotent create_file/delete_file writes.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest

from forgejo import Forgejo

_API = "http://forgejo.test/api/v1/repos/haku/haku-state"
Handler = Callable[[httpx.Request], httpx.Response]


def _call(handler: Handler, op: Callable[[Forgejo], Awaitable[Any]]) -> Any:
    """Run ``op`` against a Forgejo whose HTTP layer is the given MockTransport handler."""
    fj = Forgejo(api_url=_API, username="u", password="p")
    fj._http = httpx.AsyncClient(base_url=_API, transport=httpx.MockTransport(handler))

    async def go() -> Any:
        async with fj as f:
            return await op(f)

    return asyncio.run(go())


def test_tree_raises_on_truncated():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"tree": [], "truncated": True})

    with pytest.raises(RuntimeError, match=r"truncated|exceeded"):
        _call(handler, lambda f: f.tree("deadbeef"))


def test_blobs_are_batched_at_80_shas():
    """81 SHAs must fetch in 2 /git/blobs calls (chunk size 80), not one giant URL."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        shas = request.url.params["shas"].split(",")
        calls.append(len(shas))
        return httpx.Response(200, json=[{"sha": s, "content": base64.b64encode(s.encode()).decode()} for s in shas])

    out = _call(handler, lambda f: f.blobs([f"sha-{i}" for i in range(81)]))
    assert len(out) == 81
    assert calls == [80, 1]


def test_read_text_returns_content_or_none():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/contents/present.txt"):
            return httpx.Response(200, json={"content": base64.b64encode(b"hello").decode()})
        return httpx.Response(404)

    assert _call(handler, lambda f: f.read_text("present.txt")) == "hello"
    assert _call(handler, lambda f: f.read_text("missing.txt")) is None


def test_read_yaml_parses_or_none():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/contents/x.yaml"):
            return httpx.Response(200, json={"content": base64.b64encode(b"a: 1\nb: two\n").decode()})
        return httpx.Response(404)

    assert _call(handler, lambda f: f.read_yaml("x.yaml")) == {"a": 1, "b": "two"}
    assert _call(handler, lambda f: f.read_yaml("nope.yaml")) is None


def test_create_file_tolerates_already_exists():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        # 422 = file already exists; create_file must treat it as success (no raise).
        return httpx.Response(422, json={"message": "already exists"})

    _call(handler, lambda f: f.create_file("clicks/a/b", b"x", "msg"))  # must not raise
    assert seen == ["POST"]


def test_delete_file_is_noop_when_missing():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        return httpx.Response(404)  # GET sha → not found → no DELETE issued

    _call(handler, lambda f: f.delete_file("clicks/a/b", "msg"))  # must not raise
    assert seen == ["GET"]  # no DELETE attempted


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
