"""Talks to haku-state through the Forgejo contents API — no local clone.

Haku's UI records operator intent (clicks, feedback) as single-file commits the
Forgejo server makes for us, and reads items the same way. The cluster-internal
Forgejo is plaintext HTTP, so no TLS/CA handling. This replaces the old pygit2
clone+reconcile+push machinery: every write is one authenticated HTTP call (Forgejo
serializes commits server-side), so there is no working copy, no lock, and no pull
loop. Credentials are the haku-state-git-write basic-auth pair.

TODO(maybe): if we ever use a broad swath of the Forgejo API, swap this hand-rolled
client for ``pyforgejo`` (an OpenAPI-generated async client, itself httpx+pydantic).
For ~5 endpoints with custom 422/404 idempotency handling, hand-rolled httpx is leaner
and avoids a single-maintainer dependency.

Ported (in intent) from ``haku/console/git_state.py``.
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


class Forgejo:
    """Async client for the one haku-state repo, over the Forgejo contents API.

    ``api_url`` is the repo API root, e.g.
    ``http://forgejo-http.forgejo:3000/api/v1/repos/haku/haku-state``.
    """

    def __init__(self, *, api_url: str, username: str, password: str, branch: str = "main") -> None:
        self._http = httpx.AsyncClient(base_url=api_url, auth=(username, password), timeout=10.0)
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

    async def _list_dir(self, path: str) -> list[dict[str, Any]]:
        """Directory entries (name/type/sha/...) for ``path``; empty if it doesn't exist."""
        r = await self._http.get(f"/contents/{path}", params={"ref": self._branch})
        if r.status_code == httpx.codes.NOT_FOUND:
            return []
        r.raise_for_status()
        return r.json()

    # --- reads -------------------------------------------------------------------

    async def read_items(self) -> list[Item]:
        items: list[Item] = []
        for entry in sorted(await self._list_dir("items"), key=lambda e: e["name"]):
            if entry["type"] != "file" or not entry["name"].endswith(".yaml"):
                continue
            raw = await self._http.get(f"/raw/items/{entry['name']}", params={"ref": self._branch})
            raw.raise_for_status()
            items.append(Item.model_validate(yaml.safe_load(raw.text)))
        return items

    async def read_clicks(self) -> set[tuple[str, str]]:
        """Currently-clicked (item_id, action_id) pairs, from the clicks/ overlay."""
        clicks: set[tuple[str, str]] = set()
        for item_dir in await self._list_dir("clicks"):
            if item_dir["type"] != "dir":
                continue
            for click in await self._list_dir(f"clicks/{item_dir['name']}"):
                if click["type"] == "file":
                    clicks.add((item_dir["name"], click["name"]))
        return clicks

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
