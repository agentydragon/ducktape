"""A feature-agnostic Forgejo git-content client — no local clone, no feature knowledge.

This layer knows only about **git/Forgejo content**: reading files/trees/blobs and making
single-file commits over the Forgejo API. It deliberately knows NOTHING about haku-state's
features (items, improvements, kitchen, …) — those file paths and their parsing live in the
feature layer (`reads.py` + the endpoints in `app.py`), so adding a surface never touches
this client. The cluster-internal Forgejo is plaintext HTTP, so no TLS/CA handling. Every
write is one authenticated HTTP call (Forgejo serializes commits server-side), so there is
no working copy, no lock, and no pull loop. Credentials are the haku-state-git-write
basic-auth pair.
"""

from __future__ import annotations

import base64
from typing import Any, Self

import httpx
import yaml

# Commits the UI makes (operator clicks/feedback) are attributed to this author so callers
# can tell them apart from Haku's own scanner commits.
UI_AUTHOR = {"name": "haku-ui", "email": "haku-ui@allegedly.works"}


class Forgejo:
    """Async client for one repo over the Forgejo API. Generic content primitives only.

    ``api_url`` is the repo API root, e.g.
    ``http://forgejo-http.forgejo:3000/api/v1/repos/<owner>/<repo>``.
    """

    def __init__(self, *, api_url: str, username: str, password: str, branch: str = "main") -> None:
        self._http = httpx.AsyncClient(base_url=api_url, auth=(username, password), timeout=30.0)
        self._branch = branch

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._http.aclose()

    def _commit_body(self, message: str, *, content: bytes | None = None, sha: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"message": message, "branch": self._branch, "author": UI_AUTHOR, "committer": UI_AUTHOR}
        if content is not None:
            body["content"] = base64.b64encode(content).decode()
        if sha is not None:
            body["sha"] = sha
        return body

    # --- generic reads -----------------------------------------------------------

    async def commits(self, limit: int = 40) -> list[dict[str, Any]]:
        """Recent commits on the branch, newest first — one call. Callers derive HEAD
        (``commits[0]``) and any commit-metadata they need (e.g. a "last changed" time)."""
        r = await self._http.get("/commits", params={"sha": self._branch, "limit": str(limit)})
        r.raise_for_status()
        return r.json()

    async def tree(self, sha: str) -> list[dict[str, Any]]:
        """A commit's full recursive git tree (entries with ``path``/``type``/``sha``) — one call.

        Deliberately NOT the ``/contents/<dir>`` API: that computes a last-commit per entry
        (~0.6s/entry on this CPU-bound Forgejo), so listing a 40+ file dir serializes into 25s+
        and times out. The git trees API is a single O(tree) read with no last-commit work.
        Raises if the tree is truncated (a silent drop of files would be a correctness bug).
        """
        r = await self._http.get(f"/git/trees/{sha}", params={"recursive": "true", "per_page": "1000"})
        r.raise_for_status()
        body = r.json()
        if body["truncated"]:
            raise RuntimeError(f"tree exceeded per_page — listing would drop files ({sha=})")
        return body["tree"]

    async def blobs(self, shas: list[str]) -> list[bytes]:
        """Decoded contents for many blob SHAs via the batch ``git/blobs`` API, in input order.

        One round trip per ~80 SHAs (keeps the URL short) instead of a per-file ``/raw`` fan-out,
        which this Forgejo serializes (~1s/blob).
        """
        out: list[bytes] = []
        for i in range(0, len(shas), 80):
            r = await self._http.get("/git/blobs", params={"shas": ",".join(shas[i : i + 80])})
            r.raise_for_status()
            out.extend(base64.b64decode(b["content"]) for b in r.json())
        return out

    async def read_text(self, path: str) -> str | None:
        """A single file's text content via the contents API, or ``None`` if it doesn't exist."""
        r = await self._http.get(f"/contents/{path}", params={"ref": self._branch})
        if r.status_code == httpx.codes.NOT_FOUND:
            return None
        r.raise_for_status()
        return base64.b64decode(r.json()["content"]).decode()

    async def read_yaml(self, path: str) -> Any | None:
        """A single YAML file parsed, or ``None`` if it doesn't exist."""
        raw = await self.read_text(path)
        return None if raw is None else yaml.safe_load(raw)

    # --- generic writes (idempotent single-file commits) -------------------------
    # Forgejo makes each commit server-side; a concurrent writer just surfaces as 422
    # (file already exists) / 404 (already gone), handled inline as a no-op so these are
    # idempotent — no local reconcile-and-retry loop.

    async def create_file(self, path: str, content: bytes, message: str) -> None:
        """Create ``path`` (idempotent: a 422 'already exists' is treated as success)."""
        r = await self._http.post(f"/contents/{path}", json=self._commit_body(message, content=content))
        if r.status_code != httpx.codes.UNPROCESSABLE_ENTITY:
            r.raise_for_status()

    async def delete_file(self, path: str, message: str) -> None:
        """Delete ``path`` (idempotent: a missing file is a no-op)."""
        head = await self._http.get(f"/contents/{path}", params={"ref": self._branch})
        if head.status_code == httpx.codes.NOT_FOUND:
            return
        head.raise_for_status()
        r = await self._http.request(
            "DELETE", f"/contents/{path}", json=self._commit_body(message, sha=head.json()["sha"])
        )
        r.raise_for_status()
