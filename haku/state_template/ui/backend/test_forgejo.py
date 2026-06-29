"""The Forgejo read path — the 2-call git-tree + batched-blobs dashboard read.

Guards the parsing logic that turns a raw git tree into (items, clicks): that item
blobs are decoded and YAML-parsed, that clicks are derived from `clicks/<item>/<action>`
tree paths (and only those — not nested or malformed paths), and that a truncated tree
raises instead of silently dropping files. Forgejo itself is mocked with
httpx.MockTransport, so this is a pure unit test of forgejo.py.
"""

from __future__ import annotations

import asyncio
import base64

import httpx
import pytest
from forgejo import Forgejo

_HEAD = "deadbeefcafe"


def _item_yaml(item_id: str) -> str:
    return f'id: "{item_id}"\ntitle: "T {item_id}"\nbody: "b"\nvalue: 5\nstatus: open\n'


def _make_forgejo(tree: list[dict], blobs_by_sha: dict[str, str]) -> Forgejo:
    """A Forgejo client whose HTTP layer is a MockTransport serving the given tree/blobs."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/commits"):
            return httpx.Response(
                200,
                json=[{"sha": _HEAD, "commit": {"author": {"email": "haku@allegedly.works", "date": "2026-06-28T22:00:00Z"}}}],
            )
        if path.endswith(f"/git/trees/{_HEAD}"):
            return httpx.Response(200, json={"tree": tree, "truncated": False})
        if path.endswith("/git/blobs"):
            shas = request.url.params["shas"].split(",")
            content = [
                {"sha": s, "content": base64.b64encode(blobs_by_sha[s].encode()).decode()}
                for s in shas
            ]
            return httpx.Response(200, json=content)
        return httpx.Response(404, text=f"unexpected {path}")

    fj = Forgejo(api_url="http://forgejo.test/api/v1/repos/haku/haku-state", username="u", password="p")
    fj._http = httpx.AsyncClient(base_url="http://forgejo.test/api/v1/repos/haku/haku-state", transport=httpx.MockTransport(handler))
    return fj


def test_read_dashboard_parses_items_and_clicks():
    tree = [
        {"type": "blob", "path": "items/01AAA.yaml", "sha": "sha-a"},
        {"type": "blob", "path": "items/01BBB.yaml", "sha": "sha-b"},
        {"type": "blob", "path": "README.md", "sha": "sha-readme"},  # non-item blob, ignored
        {"type": "tree", "path": "items", "sha": "sha-tree"},  # tree entry, ignored
        {"type": "blob", "path": "clicks/01AAA/done", "sha": "sha-click"},
        {"type": "blob", "path": "clicks/01AAA", "sha": "sha-bad1"},  # too shallow, ignored
        {"type": "blob", "path": "clicks/01AAA/nested/deep", "sha": "sha-bad2"},  # too deep, ignored
    ]
    blobs = {"sha-a": _item_yaml("01AAA"), "sha-b": _item_yaml("01BBB")}

    async def run():
        async with _make_forgejo(tree, blobs) as fj:
            return await fj.read_dashboard()

    items, clicks, scan_time = asyncio.run(run())
    assert scan_time == "2026-06-28T22:00:00Z"
    assert {i.id for i in items} == {"01AAA", "01BBB"}
    assert clicks == {("01AAA", "done")}


def test_read_dashboard_raises_on_truncated_tree():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commits"):
            return httpx.Response(
                200, json=[{"sha": _HEAD, "commit": {"author": {"email": "haku@allegedly.works", "date": "2026-06-28T22:00:00Z"}}}]
            )
        return httpx.Response(200, json={"tree": [], "truncated": True})

    fj = Forgejo(api_url="http://forgejo.test/api/v1/repos/haku/haku-state", username="u", password="p")
    fj._http = httpx.AsyncClient(base_url="http://forgejo.test", transport=httpx.MockTransport(handler))

    async def run():
        async with fj as f:
            await f.read_dashboard()

    with pytest.raises(RuntimeError, match="truncated|exceeded"):
        asyncio.run(run())


def test_blobs_are_batched_at_80_shas():
    """81 items must fetch in 2 /git/blobs calls (chunk size 80), not one giant URL."""
    tree = [{"type": "blob", "path": f"items/{i:05d}.yaml", "sha": f"sha-{i}"} for i in range(81)]
    blobs = {f"sha-{i}": _item_yaml(f"{i:05d}") for i in range(81)}
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/commits"):
            return httpx.Response(
                200,
                json=[{"sha": _HEAD, "commit": {"author": {"email": "haku@allegedly.works", "date": "2026-06-28T22:00:00Z"}}}],
            )
        if path.endswith(f"/git/trees/{_HEAD}"):
            return httpx.Response(200, json={"tree": tree, "truncated": False})
        if path.endswith("/git/blobs"):
            shas = request.url.params["shas"].split(",")
            calls.append(len(shas))
            return httpx.Response(
                200,
                json=[{"sha": s, "content": base64.b64encode(blobs[s].encode()).decode()} for s in shas],
            )
        return httpx.Response(404)

    fj = Forgejo(api_url="http://forgejo.test/api/v1/repos/haku/haku-state", username="u", password="p")
    fj._http = httpx.AsyncClient(base_url="http://forgejo.test", transport=httpx.MockTransport(handler))

    async def run():
        async with fj as f:
            return await f.read_dashboard()

    items, _, _ = asyncio.run(run())
    assert len(items) == 81
    assert calls == [80, 1]


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
