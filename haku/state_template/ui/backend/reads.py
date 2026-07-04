"""Feature reads — compose the generic Forgejo client into haku-state's data shapes.

This is the layer that DOES know about features (which file/dir, how to parse it). It sits
above the feature-agnostic `Forgejo` client so that adding a surface changes only this layer
(and the endpoint), never the git-content client. Simple single-file reads are just
`forgejo.read_yaml(path)` inline in the endpoint; surfaces that need the batched tree+blobs read
(e.g. the runs manifests) live here. Content collections (e.g. items, improvements) skip this
layer entirely — the frontend composes over the generic tree+blobs proxy.
"""

from __future__ import annotations

import yaml
from forgejo import Forgejo
from models import RunManifest

# Haku-the-scanner commits as this author; the "last scan" time is the newest such commit
# (NOT the UI's response/feedback writes, NOT Flux image-automation commits).
_HAKU_AUTHOR_EMAIL = "haku@example.com"


async def read_scan_time(forgejo: Forgejo) -> str:
    """When haku-state last meaningfully changed (ISO 8601): the newest Haku-authored commit,
    surfaced to the UI footer as "last scan". Items themselves are read from `items/*.md` via the
    generic proxy — the frontend composes them; this is just the freshness stamp."""
    commits = await forgejo.commits()
    return next(
        (c["commit"]["author"]["date"] for c in commits if c["commit"]["author"]["email"] == _HAKU_AUTHOR_EMAIL),
        commits[0]["commit"]["author"]["date"],
    )


async def read_runs(forgejo: Forgejo, limit: int = 20) -> list[RunManifest]:
    """Recent per-run propagation manifests (``runs/<date>/<ulid>.yaml``) with their prose
    notes (the sibling ``.md``) attached, newest-first by ``started``.

    Two reads beyond the commits list (tree + one batched blobs fetch), regardless of run
    count. A run needs a ``.yaml`` to appear; the ``.md``
    is optional (``runs/README.md`` and other dangling ``.md`` are ignored)."""
    commits = await forgejo.commits(1)
    tree = await forgejo.tree(commits[0]["sha"])
    pairs: dict[str, dict[str, str]] = {}  # run base path -> {"yaml": sha, "md": sha}
    for e in tree:
        if e["type"] != "blob" or not e["path"].startswith("runs/"):
            continue
        path = e["path"]
        if path.endswith(".yaml"):
            pairs.setdefault(path.removesuffix(".yaml"), {})["yaml"] = e["sha"]
        elif path.endswith(".md") and not path.endswith("/README.md"):
            pairs.setdefault(path.removesuffix(".md"), {})["md"] = e["sha"]
    bases = [b for b, s in pairs.items() if "yaml" in s]
    shas = [sha for b in bases for sha in (pairs[b]["yaml"], *([pairs[b]["md"]] if "md" in pairs[b] else []))]
    blobs = iter(await forgejo.blobs(shas))
    runs: list[RunManifest] = []
    for b in bases:
        manifest = yaml.safe_load(next(blobs)) or {}
        manifest["notes_md"] = next(blobs).decode() if "md" in pairs[b] else ""
        runs.append(RunManifest.model_validate(manifest))
    runs.sort(key=lambda m: m.started, reverse=True)
    return runs[:limit]
