"""The generic Forgejo content client — git primitives, no feature knowledge.

Forgejo is mocked with httpx.MockTransport, so these are pure unit tests of forgejo.py:
the batched tree/blobs reads (incl. the truncated-tree guard, sub-cap chunking, and by-SHA
mapping), single-file read_text/read_yaml, and the idempotent create/write/delete_file writes.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
import pytest_bazel
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


def test_blobs_are_batched_below_the_forgejo_cap():
    """81 SHAs fetch in chunks of 40 — Forgejo silently caps /git/blobs at 50, so we stay under it."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        shas = request.url.params["shas"].split(",")
        calls.append(len(shas))
        return httpx.Response(200, json=[{"sha": s, "content": base64.b64encode(s.encode()).decode()} for s in shas])

    out = _call(handler, lambda f: f.blobs([f"sha-{i}" for i in range(81)]))
    assert len(out) == 81
    assert calls == [40, 40, 1]
    assert all(chunk <= 50 for chunk in calls)


def test_blobs_map_by_sha_not_response_order():
    """A response that reorders the blobs must not scramble the input→content mapping."""

    def handler(request: httpx.Request) -> httpx.Response:
        shas = request.url.params["shas"].split(",")
        body = [{"sha": s, "content": base64.b64encode(f"body-{s}".encode()).decode()} for s in reversed(shas)]
        return httpx.Response(200, json=body)

    out = _call(handler, lambda f: f.blobs(["a", "b", "c"]))
    assert out == [b"body-a", b"body-b", b"body-c"]


def test_blobs_raise_when_server_drops_one():
    """If the batch endpoint silently omits a requested SHA, fail loud rather than dropping it —
    this is exactly the cap bug that hid the 51st+ item from the dashboard."""

    def handler(request: httpx.Request) -> httpx.Response:
        kept = request.url.params["shas"].split(",")[:-1]  # simulate the cap dropping the last one
        return httpx.Response(200, json=[{"sha": s, "content": base64.b64encode(s.encode()).decode()} for s in kept])

    with pytest.raises(RuntimeError, match="git/blobs returned"):
        _call(handler, lambda f: f.blobs(["a", "b", "c"]))


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

    _call(handler, lambda f: f.create_file("responses/dentist-appt/status.yaml", b"x", "msg"))  # must not raise
    assert seen == ["POST"]


def test_write_file_posts_when_absent_and_puts_when_present():
    """Upsert: POST (create) when the file is missing, PUT (overwrite, with sha) when present."""
    absent: list[str] = []

    def absent_handler(request: httpx.Request) -> httpx.Response:
        absent.append(request.method)
        if request.method == "GET":
            return httpx.Response(404)  # file missing → create via POST
        return httpx.Response(200, json={})

    _call(absent_handler, lambda f: f.write_file("responses/dentist-appt/status.yaml", b"x", "msg"))
    assert absent == ["GET", "POST"]

    present: list[tuple[str, Any]] = []

    def present_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"sha": "old-sha"})
        present.append((request.method, request.read()))
        return httpx.Response(200, json={})

    _call(present_handler, lambda f: f.write_file("responses/dentist-appt/status.yaml", b"x", "msg"))
    method, raw = present[0]
    assert method == "PUT"
    assert b'"sha":"old-sha"' in raw.replace(b" ", b"")  # PUT carries the existing sha


def test_delete_file_is_noop_when_missing():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        return httpx.Response(404)  # GET sha → not found → no DELETE issued

    _call(handler, lambda f: f.delete_file("responses/dentist-appt/status.yaml", "msg"))  # must not raise
    assert seen == ["GET"]  # no DELETE attempted


if __name__ == "__main__":
    pytest_bazel.main()
