"""The feature reads (`reads.read_scan_time`) — composing Forgejo primitives.

Guards the "last scan" selection: the freshness stamp is the newest **Haku-authored** commit,
skipping the UI's own response/feedback writes and Flux image-automation commits. Items
themselves are read from `items/*.md` client-side over the generic proxy — not here.
Forgejo is mocked with httpx.MockTransport (the generic primitives have their own unit tests).
"""

from __future__ import annotations

import asyncio

import httpx
from forgejo import Forgejo
from reads import read_scan_time

_API = "http://forgejo.test/api/v1/repos/haku/haku-state"


def _forgejo(commits: list[dict]) -> Forgejo:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commits"):
            return httpx.Response(200, json=commits)
        return httpx.Response(404, text=f"unexpected {request.url.path}")

    fj = Forgejo(api_url=_API, username="u", password="p")
    fj._http = httpx.AsyncClient(base_url=_API, transport=httpx.MockTransport(handler))
    return fj


def _run(commits: list[dict]) -> str:
    async def go():
        async with _forgejo(commits) as f:
            return await read_scan_time(f)

    return asyncio.run(go())


def test_scan_time_is_newest_haku_authored_commit():
    # Newest commit is a UI write; the scan time must skip it and pick the newest Haku commit.
    scan_time = _run(
        [
            {"commit": {"author": {"email": "haku-ui@example.com", "date": "2026-06-28T23:00:00Z"}}},
            {"commit": {"author": {"email": "haku@example.com", "date": "2026-06-28T22:00:00Z"}}},
        ]
    )
    assert scan_time == "2026-06-28T22:00:00Z"


def test_scan_time_falls_back_to_newest_commit_when_no_haku_author():
    # No Haku-authored commit → fall back to the newest commit rather than raising.
    scan_time = _run([{"commit": {"author": {"email": "flux@example.com", "date": "2026-06-28T23:00:00Z"}}}])
    assert scan_time == "2026-06-28T23:00:00Z"


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-q"])
