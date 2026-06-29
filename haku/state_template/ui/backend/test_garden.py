"""The knowledge-garden read + path guard.

`read_garden_index` lists only `.md`/`.mdx` under the whitelisted `GARDEN_DIRS`, sorted by path;
`_garden_path` rejects anything off the whitelist (other dirs, traversal, non-markdown) so the
endpoint can't be coaxed into serving an arbitrary repo file. Forgejo is mocked with MockTransport.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from app import _garden_path
from fastapi import HTTPException
from forgejo import Forgejo
from reads import read_garden_index

_API = "http://forgejo.test/api/v1/repos/haku/haku-state"
_HEAD = "deadbeefcafe"


def _forgejo(tree: list[dict]) -> Forgejo:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/commits"):
            return httpx.Response(200, json=[{"sha": _HEAD, "commit": {"author": {"email": "x", "date": "d"}}}])
        if path.endswith(f"/git/trees/{_HEAD}"):
            return httpx.Response(200, json={"tree": tree, "truncated": False})
        return httpx.Response(404, text=f"unexpected {path}")

    fj = Forgejo(api_url=_API, username="u", password="p")
    fj._http = httpx.AsyncClient(base_url=_API, transport=httpx.MockTransport(handler))
    return fj


def test_read_garden_index_lists_only_whitelisted_markdown_sorted():
    tree = [
        {"type": "blob", "path": "memory/situational-awareness.md", "sha": "1"},
        {"type": "blob", "path": "procedures/propagation/kitchen.md", "sha": "2"},
        {"type": "blob", "path": "runs/2026-06-29/01X.md", "sha": "3"},
        {"type": "blob", "path": "runs/2026-06-29/01X.yaml", "sha": "4"},  # not markdown → excluded
        {"type": "blob", "path": "items/01Y.yaml", "sha": "5"},  # off-whitelist dir → excluded
        {"type": "blob", "path": "intake/secret.md", "sha": "6"},  # off-whitelist dir → excluded
        {"type": "tree", "path": "memory", "sha": "7"},  # a tree entry, not a blob → excluded
    ]

    async def go():
        async with _forgejo(tree) as f:
            return await read_garden_index(f)

    entries = asyncio.run(go())
    assert [e.path for e in entries] == [
        "memory/situational-awareness.md",
        "procedures/propagation/kitchen.md",
        "runs/2026-06-29/01X.md",
    ]


@pytest.mark.parametrize("good", ["memory/x.md", "procedures/a/b.mdx", "runs/2026-06-29/01X.md"])
def test_garden_path_accepts_whitelisted_markdown(good):
    assert _garden_path(good) == good


@pytest.mark.parametrize(
    "bad",
    [
        "items/01Y.yaml",  # off-whitelist dir
        "memory/../intake/secret.md",  # traversal
        "/etc/passwd",  # absolute
        "memory/board.yaml",  # not markdown
        "memoryx/x.md",  # prefix-trick: not actually under memory/
    ],
)
def test_garden_path_rejects_off_whitelist(bad):
    with pytest.raises(HTTPException):
        _garden_path(bad)


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
