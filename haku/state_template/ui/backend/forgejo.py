"""Talks to haku-state through the Forgejo contents API — no local clone.

Haku's UI records operator intent (clicks, feedback) as single-file commits the
Forgejo server makes for us, and reads items the same way. The cluster-internal
Forgejo is plaintext HTTP, so no TLS/CA handling. Every write is one authenticated
HTTP call (Forgejo serializes commits server-side), so there is no working copy, no
lock, and no pull loop. Credentials are the haku-state-git-write basic-auth pair.
"""

from __future__ import annotations

import base64
import datetime as dt
import logging
from typing import Any, Self

import httpx
import yaml
from models import Item

logger = logging.getLogger(__name__)

# Commits attributed to the UI so Haku can tell them apart from its own runs.
_AUTHOR = {"name": "haku-ui", "email": "haku-ui@allegedly.works"}
# Haku-the-scanner commits as this author; the "last scan" time is the newest such commit
# (NOT the UI's click/feedback writes, NOT Flux image-automation commits).
_HAKU_AUTHOR_EMAIL = "haku@allegedly.works"


class Forgejo:
    """Async client for the one haku-state repo, over the Forgejo contents API.

    ``api_url`` is the repo API root, e.g.
    ``http://forgejo-http.forgejo:3000/api/v1/repos/haku/haku-state``.
    """

    def __init__(self, *, api_url: str, username: str, password: str, branch: str = "main") -> None:
        self._http = httpx.AsyncClient(base_url=api_url, auth=(username, password), timeout=30.0)
        self._branch = branch

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._http.aclose()

    def _commit(self, message: str, *, content: bytes | None = None, sha: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"message": message, "branch": self._branch, "author": _AUTHOR, "committer": _AUTHOR}
        if content is not None:
            body["content"] = base64.b64encode(content).decode()
        if sha is not None:
            body["sha"] = sha
        return body

    async def _commits(self, limit: int = 40) -> list[dict[str, Any]]:
        """Recent commits on the branch (newest first) — one call. Used for both the HEAD sha
        (commits[0]) and the last *Haku-authored* scan time."""
        r = await self._http.get("/commits", params={"sha": self._branch, "limit": str(limit)})
        r.raise_for_status()
        return r.json()

    async def _tree(self, head: str) -> list[dict[str, Any]]:
        """The given commit's full recursive git tree — one call.

        Deliberately NOT the ``/contents/<dir>`` API: that computes a last-commit per entry
        (~0.6s/entry on this CPU-bound Forgejo), so listing items/ (40+ files) serializes into
        25s+ and times out. The git trees API is a single O(tree) read with no last-commit work.
        """
        r = await self._http.get(f"/git/trees/{head}", params={"recursive": "true", "per_page": "1000"})
        r.raise_for_status()
        body = r.json()
        if body["truncated"]:
            raise RuntimeError(f"haku-state tree exceeded per_page — listing would drop files ({head=})")
        return body["tree"]

    async def _blobs(self, shas: list[str]) -> list[bytes]:
        """Decoded contents for many blob SHAs via the batch ``git/blobs`` API.

        One round trip per ~80 SHAs (keeps the URL short) instead of a per-file ``/raw`` fan-out,
        which this Forgejo serializes (~1s/blob → ~50s for the item set).
        """
        out: list[bytes] = []
        for i in range(0, len(shas), 80):
            r = await self._http.get("/git/blobs", params={"shas": ",".join(shas[i : i + 80])})
            r.raise_for_status()
            out.extend(base64.b64decode(b["content"]) for b in r.json())
        return out

    # --- reads -------------------------------------------------------------------

    async def read_dashboard(self) -> tuple[list[Item], set[tuple[str, str]], str]:
        """Items, currently-clicked (item_id, action_id) pairs, and the HEAD commit timestamp.

        The timestamp (ISO 8601) is when haku-state last changed — i.e. the last scan/update —
        surfaced to the UI as the "last scan" time. Two reads beyond the commits list regardless
        of item count: the git tree (paths + blob SHAs) and a batched ``git/blobs`` fetch. The
        clicks/ overlay is read straight off the tree paths, no extra calls.
        """
        commits = await self._commits()
        head_sha = commits[0]["sha"]
        # "Last scan" = newest commit Haku itself authored (skip UI writes / Flux image bumps);
        # fall back to the newest commit if none of the recent ones are Haku's.
        scan_time = next(
            (c["commit"]["author"]["date"] for c in commits if c["commit"]["author"]["email"] == _HAKU_AUTHOR_EMAIL),
            commits[0]["commit"]["author"]["date"],
        )
        tree = await self._tree(head_sha)
        item_shas = [
            e["sha"]
            for e in tree
            if e["type"] == "blob" and e["path"].startswith("items/") and e["path"].endswith(".yaml")
        ]
        clicks: set[tuple[str, str]] = set()
        for e in tree:
            parts = e["path"].split("/")
            if e["type"] == "blob" and parts[0] == "clicks" and len(parts) == 3:
                clicks.add((parts[1], parts[2]))
        items = [Item.model_validate(yaml.safe_load(raw)) for raw in await self._blobs(item_shas)]
        return items, clicks, scan_time

    async def read_improvements(self) -> dict[str, Any] | None:
        """The self-backlog (`improvements.yaml`): capability ideas + friction. One file,
        read via tree+blob like the dashboard. None if the file isn't present yet."""
        commits = await self._commits(1)
        tree = await self._tree(commits[0]["sha"])
        sha = next((e["sha"] for e in tree if e["type"] == "blob" and e["path"] == "improvements.yaml"), None)
        if sha is None:
            return None
        (raw,) = await self._blobs([sha])
        return yaml.safe_load(raw)

    # --- writes (clicks/ overlay + free-form feedback) ---------------------------
    # The UI never edits items/ (Haku owns those). A clicked action is the file
    # clicks/<item_id>/<action_id> (removed on un-click); Haku reduces these on its
    # next run. Forgejo makes each commit server-side; a concurrent Haku push just
    # surfaces as 422 (file already exists) / 404 (already gone), handled inline
    # instead of a local reconcile-and-retry loop.

    async def set_click(self, item_id: str, action_id: str) -> None:
        stamp = f"clicked_at: {dt.datetime.now(dt.UTC).isoformat(timespec='seconds')}\n"
        r = await self._http.post(
            f"/contents/clicks/{item_id}/{action_id}",
            json=self._commit(f"ui: click {action_id} on {item_id}", content=stamp.encode()),
        )
        if r.status_code != httpx.codes.UNPROCESSABLE_ENTITY:  # already exists → already clicked
            r.raise_for_status()

    async def clear_click(self, item_id: str, action_id: str) -> None:
        path = f"/contents/clicks/{item_id}/{action_id}"
        head = await self._http.get(path, params={"ref": self._branch})
        if head.status_code == httpx.codes.NOT_FOUND:  # already cleared
            return
        head.raise_for_status()
        r = await self._http.request(
            "DELETE", path, json=self._commit(f"ui: unclick {action_id} on {item_id}", sha=head.json()["sha"])
        )
        r.raise_for_status()

    async def write_feedback(self, text: str, item_id: str | None = None) -> None:
        # Per-item feedback names the item id in the note + filename; Haku's run
        # contract treats an intake note referencing an item id as feedback on it.
        stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        suffix = f"-{item_id}" if item_id else ""
        heading = f"Operator feedback on {item_id}" if item_id else "Operator feedback"
        message = f"ui: feedback on {item_id}" if item_id else "ui: feedback"
        body = f"# {heading} ({stamp})\n\n{text.strip()}\n"
        r = await self._http.post(
            f"/contents/intake/{stamp}-feedback{suffix}.md", json=self._commit(message, content=body.encode())
        )
        r.raise_for_status()
