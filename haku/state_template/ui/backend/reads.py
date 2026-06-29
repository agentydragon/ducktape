"""Feature reads — compose the generic Forgejo client into haku-state's data shapes.

This is the layer that DOES know about features (which file/dir, how to parse it). It sits
above the feature-agnostic `Forgejo` client so that adding a surface changes only this layer
(and the endpoint), never the git-content client. Simple single-file reads (e.g. the
Improvements board) are just `forgejo.read_yaml(path)` inline in the endpoint; the items
board needs the batched tree+blobs read, which lives here.
"""

from __future__ import annotations

import yaml
from forgejo import Forgejo
from models import Item

# Haku-the-scanner commits as this author; the "last scan" time is the newest such commit
# (NOT the UI's click/feedback writes, NOT Flux image-automation commits).
_HAKU_AUTHOR_EMAIL = "haku@allegedly.works"


async def read_dashboard(forgejo: Forgejo) -> tuple[list[Item], set[tuple[str, str]], str]:
    """Items, currently-clicked ``(item_id, action_id)`` pairs, and the last-scan timestamp.

    The timestamp (ISO 8601) is when haku-state last meaningfully changed — the newest
    Haku-authored commit — surfaced to the UI as "last scan". Beyond the commits list this is
    two reads regardless of item count: the git tree (paths + blob SHAs) and a batched blobs
    fetch. The clicks/ overlay is derived straight off the tree paths, no extra calls.
    """
    commits = await forgejo.commits()
    head_sha = commits[0]["sha"]
    scan_time = next(
        (c["commit"]["author"]["date"] for c in commits if c["commit"]["author"]["email"] == _HAKU_AUTHOR_EMAIL),
        commits[0]["commit"]["author"]["date"],
    )
    tree = await forgejo.tree(head_sha)
    item_shas = [
        e["sha"] for e in tree if e["type"] == "blob" and e["path"].startswith("items/") and e["path"].endswith(".yaml")
    ]
    clicks: set[tuple[str, str]] = set()
    for e in tree:
        parts = e["path"].split("/")
        if e["type"] == "blob" and parts[0] == "clicks" and len(parts) == 3:
            clicks.add((parts[1], parts[2]))
    items = [Item.model_validate(yaml.safe_load(raw)) for raw in await forgejo.blobs(item_shas)]
    return items, clicks, scan_time
