"""The items-board feature read (`reads.read_dashboard`) — composing the Forgejo primitives.

Guards the parsing that turns a raw git tree into (items, clicks, scan_time): item blobs are
decoded + YAML-parsed, clicks derive only from well-formed `clicks/<item>/<action>` tree paths,
and the scan time is the newest Haku-authored commit. Forgejo is mocked with MockTransport.
"""

from __future__ import annotations

import asyncio
import base64

import httpx
from forgejo import Forgejo
from reads import read_dashboard

_API = "http://forgejo.test/api/v1/repos/haku/haku-state"
_HEAD = "deadbeefcafe"


def _item_yaml(item_id: str) -> str:
    return f'id: "{item_id}"\ntitle: "T {item_id}"\nbody: "b"\nvalue: 5\nstatus: open\n'


def test_read_dashboard_parses_items_clicks_and_scan_time():
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

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/commits"):
            # First commit is a UI write; the scan time must skip it and pick the Haku commit.
            return httpx.Response(
                200,
                json=[
                    {
                        "sha": _HEAD,
                        "commit": {"author": {"email": "haku-ui@allegedly.works", "date": "2026-06-28T23:00:00Z"}},
                    },
                    {
                        "sha": "older",
                        "commit": {"author": {"email": "haku@allegedly.works", "date": "2026-06-28T22:00:00Z"}},
                    },
                ],
            )
        if path.endswith(f"/git/trees/{_HEAD}"):
            return httpx.Response(200, json={"tree": tree, "truncated": False})
        if path.endswith("/git/blobs"):
            shas = request.url.params["shas"].split(",")
            return httpx.Response(
                200, json=[{"sha": s, "content": base64.b64encode(blobs[s].encode()).decode()} for s in shas]
            )
        return httpx.Response(404, text=f"unexpected {path}")

    fj = Forgejo(api_url=_API, username="u", password="p")
    fj._http = httpx.AsyncClient(base_url=_API, transport=httpx.MockTransport(handler))

    async def go():
        async with fj as f:
            return await read_dashboard(f)

    items, clicks, scan_time = asyncio.run(go())
    assert {i.id for i in items} == {"01AAA", "01BBB"}
    assert clicks == {("01AAA", "done")}
    assert scan_time == "2026-06-28T22:00:00Z"  # newest Haku-authored, not the newer UI write


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-q"])
